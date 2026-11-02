# 功能：UserInputTool 接线完整性契约（DoD-11）——registry 工具 bind_ui 装配
# 背景：registry 内 UserInputTool 裸实例化（factory 无 ui），若 run_agent_turn 注入的
# user_interaction 不 bind 到工具，真实链 LLM 调用 request_user_input 会 fail-closed
# 抛错而非真挂起（接线盲区实证）。契约钉：build_agent_context(user_interaction=X) 后
# registry 内 request_user_input._ui is X；未装配时保持 None（headless fail-closed）。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bandi.agent.factory import build_agent_context
from bandi.core.config import BandiConfig


class _FakeUI:
    """最小 async UserInteraction 实现（结构协议）。"""

    async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
        return True

    async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
        return True

    async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
        return "fake answer"

    async def show_notification(self, message: str, level: str = "info") -> None:
        pass


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""  # 强制 MockProvider，不触真实 LLM
    with patch("bandi.core.config.BandiConfig.load", return_value=cfg):
        yield


def _request_user_input_tool(ctx) -> object:
    from bandi.tools.user_input import UserInputTool

    tool = ctx.tool_executor.registry.get("request_user_input")
    assert isinstance(tool, UserInputTool)
    return tool


@pytest.mark.asyncio
async def test_build_agent_context_binds_ui_to_registry_tool() -> None:
    """注入 user_interaction 后 registry 内 request_user_input 必须拿到同一 UI 实例。"""
    ui = _FakeUI()
    ctx = build_agent_context(
        user_input="hi",
        mode="execute",
        user_interaction=ui,  # type: ignore[arg-type]
    )
    tool = _request_user_input_tool(ctx)
    assert tool._ui is ui, (
        "真实链 request_user_input 必须拿到注入的交互通道（否则 fail-closed 抛错非挂起）"
    )


@pytest.mark.asyncio
async def test_without_ui_tool_stays_unbound() -> None:
    """未装配 UI（headless）→ 工具保持 None——fail-closed 语义不丢。"""
    ctx = build_agent_context(user_input="hi", mode="execute")
    tool = _request_user_input_tool(ctx)
    assert tool._ui is None


@pytest.mark.asyncio
async def test_bound_tool_suspends_via_injected_ui() -> None:
    """接线后行为族：bind 的 UI 决定工具提问走注入通道（不 fail-closed）。"""
    import asyncio

    from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore

    store = InteractionStore()
    frames: list = []

    async def _push(item) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, session_id="w1", on_interaction=_push)
    ctx = build_agent_context(
        user_input="hi",
        mode="execute",
        user_interaction=ui,  # type: ignore[arg-type]
    )
    tool = _request_user_input_tool(ctx)
    task = asyncio.create_task(
        tool.execute({"reason": "need", "questions": [{"id": "q", "question": "仓库?"}]})
    )
    await asyncio.sleep(0)
    assert task.done() is False, "bind 后工具提问必须真挂起"
    assert len(frames) == 1
    store.resolve(frames[0].interaction_id, "https://bind.example")
    result = await task
    assert result["answers"]["q"] == "https://bind.example"


@pytest.mark.asyncio
async def test_real_executor_dispatch_suspends_and_resumes() -> None:
    """真实 ToolExecutor 分派：ToolCall → middleware → UserInputTool → 真挂起 → resolve → ToolResult。

    钉最终产物：executor.execute 返回的 ToolResult 携带用户答复 answers（生产主链路分派）。
    """
    import asyncio

    from bandi.core.message import ToolCall
    from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore

    store = InteractionStore()
    frames: list = []

    async def _push(item) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, session_id="w2", on_interaction=_push)
    ctx = build_agent_context(
        user_input="hi",
        mode="execute",
        user_interaction=ui,  # type: ignore[arg-type]
    )
    executor = ctx.tool_executor
    assert _request_user_input_tool(ctx)._ui is ui, "executor 内 registry 工具已 bind"

    tc = ToolCall(
        id="exec-1",
        name="request_user_input",
        arguments={"reason": "need", "questions": [{"id": "q", "question": "仓库地址?"}]},
    )
    task = asyncio.create_task(executor.execute([tc]))
    await asyncio.sleep(0.2)
    assert task.done() is False, "真实 executor 分派后必须真挂起（非失败返回）"
    assert frames and frames[0].kind == "input"

    store.resolve(frames[0].interaction_id, "https://executor.example")
    results = await asyncio.wait_for(task, timeout=5)
    assert len(results) == 1
    assert results[0].success is True
    assert "https://executor.example" in str(results[0].output)
