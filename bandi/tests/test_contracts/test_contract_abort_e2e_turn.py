"""功能：中止端到端——真实轮次挂起在待决策交互上时，abort 必须让轮次迅速终结。

背景：`POST /api/sessions/{id}/abort` 原实现只调 `controller.abort()`，而挂起中的轮次协程
停在 `InteractionStore.get_decision()` 等 resolve/reject，**收不到 abort 信号** ⇒ 只能等
TTL 到期才出终态帧（表现为「点了停止仍一直『正在回复中…』」）。

本契约走**生产主链路**（非桩）：
  - `run_agent_turn`（生产入口：真实 tool pipeline / 权限询问 / 交互通道）
  - `HTTPUserInteraction` + `app._get_interaction_store()`（生产单例）
  - `session_abort`（端点函数本体）
仅 LLM 用 MockProvider 产出一个 `write` 工具调用，以确定性触发权限询问并挂起。

断言三件事（均为「最终产物」级）：
  1. 对照组（不 abort）轮次确实**停住不自行终结**——证明挂起语义成立，实验组的快速终结
     只能归因于 abort；
  2. 实验组 abort 后轮次在秒级终结，且终态为 `USER_INTERRUPT`；
  3. 被中止的写入**不得落盘**（拒绝不得被解释为放行）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import ToolCall
from bandi.llm.mock import MockProvider

app_module = importlib.import_module("bandi.api.app")

# 对照组静置时长（秒）：足以证伪「轮次会自行很快终结」；远小于 TTL
PARK_WINDOW_SEC = 3.0
# 实验组终结预算（秒）：修复前该值需 ≈（TTL），故 上限具备判别力
ABORT_BUDGET_SEC = 20.0


class _ToolCallProvider(MockProvider):
    """功能：stream() 固定产出一个 write 工具调用（确定性触发权限询问）。"""

    def __init__(self, target: str) -> None:
        super().__init__()
        self._target = target

    async def stream(  # type: ignore[override]
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kwargs: Any
    ) -> Any:
        from bandi.llm.streaming import StreamEvent, StreamEventType

        tc = ToolCall(id="call-1", name="write", arguments={"path": self._target, "content": "X"})
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_START, tool_call_id=tc.id, tool_name=tc.name
        )
        yield StreamEvent(
            type=StreamEventType.TOOL_CALL_DELTA,
            tool_call_id=tc.id,
            arguments_delta=json.dumps(tc.arguments, ensure_ascii=False),
        )
        yield StreamEvent(type=StreamEventType.TOOL_CALL_END, tool_call_id=tc.id)
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """功能：隔离 home 与交互单例（默认路径必须 tmp + 单例重置双保险）。"""
    from bandi.core.user_interaction import InteractionStore

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(app_module, "_interaction_store", InteractionStore())
    app_module._active_abort_controllers.clear()
    yield tmp_path
    app_module._active_abort_controllers.clear()


async def _park_turn(store: Any, session_id: str, target: Path) -> tuple[Any, Any]:
    """起一个真实轮次，等它挂起在 pending 交互上；返回 (task, store_ui)。"""
    from bandi.core.abort_controller import AbortController
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.user_interaction import HTTPUserInteraction

    store_ui = app_module._get_interaction_store()
    http_ui = HTTPUserInteraction(store_ui, session_id=session_id)
    ctrl = AbortController()
    app_module._active_abort_controllers[session_id] = ctrl
    task = asyncio.create_task(
        run_agent_turn(
            user_input="写文件",
            mode="execute",
            session_id=session_id,
            session_store=store,
            abort_controller=ctrl,
            user_interaction=http_ui,
        )
    )
    deadline = asyncio.get_running_loop().time() + 90
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
        if store_ui.list_pending(session_id=session_id) or task.done():
            break
    return task, store_ui


@pytest.mark.asyncio
async def test_abort_terminates_turn_parked_on_interaction(isolated: Path) -> None:
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(isolated / "sess.db"))

    # ── 对照组：不 abort，轮次必须停住（挂起语义成立） ──
    sid_a = store.create_session(mode="execute")
    with patch(
        "bandi.core.agent_runner.build_provider",
        return_value=_ToolCallProvider(str(isolated / "a.txt")),
    ):
        task_a, ui_a = await _park_turn(store, sid_a, isolated / "a.txt")
        assert ui_a.list_pending(session_id=sid_a), "前置：必须挂起在待决策交互上"
        assert not task_a.done(), "前置：挂起中轮次不应已终结"
        await asyncio.sleep(PARK_WINDOW_SEC)
        assert not task_a.done(), (
            f"对照组：静置 {PARK_WINDOW_SEC}s 轮次就自行终结，说明未真正挂起，实验组结论将不可归因"
        )
        task_a.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task_a
        for leftover in ui_a.list_pending(session_id=sid_a):
            ui_a.reject(leftover.interaction_id)

    # ── 实验组：abort 后必须秒级终结 ──
    sid_b = store.create_session(mode="execute")
    with patch(
        "bandi.core.agent_runner.build_provider",
        return_value=_ToolCallProvider(str(isolated / "b.txt")),
    ):
        task_b, ui_b = await _park_turn(store, sid_b, isolated / "b.txt")
        assert ui_b.list_pending(session_id=sid_b), "前置：必须挂起"
        assert not task_b.done(), "前置：挂起中轮次不应已终结"

        started = asyncio.get_running_loop().time()
        result = await app_module.session_abort(sid_b)
        assert result["aborted"] is True
        assert result["rejected_interactions"], "abort 必须拒绝挂起交互（否则轮次学不到中止）"

        await asyncio.wait({task_b}, timeout=ABORT_BUDGET_SEC)
        elapsed = asyncio.get_running_loop().time() - started

        assert task_b.done(), (
            f"abort 后 {ABORT_BUDGET_SEC}s 轮次仍未终结——挂起点未被解开（修复前的 TTL 等待行为）"
        )
        assert task_b.result().terminal_reason == "USER_INTERRUPT", (
            f"中止终态必须是 USER_INTERRUPT，实际={task_b.result().terminal_reason!r}"
        )
        assert elapsed < ABORT_BUDGET_SEC
        assert not (isolated / "b.txt").exists(), (
            "被中止的写入不得落盘——拒绝不得被解释为放行（安全断言）"
        )
