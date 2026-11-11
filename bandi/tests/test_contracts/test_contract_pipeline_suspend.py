# 功能：Day132c 真挂起端到端契约——PermissionsPipeline × HTTPUserInteraction × InteractionStore
# 背景：核心设计验证「挂起落在工具执行路径内，AgentLoop 状态机零侵入」。
# → register + await get_decision（真挂起）→ 用户决策 resolve 唤醒 → check 返回 allow。
# 契约钉行为：同 turn 挂起→唤醒闭环、超时 fail-closed deny、拒绝 deny、confirm 缓存族不变。
from __future__ import annotations

import asyncio

import pytest

from bandi.core.message import ToolCall
from bandi.core.user_interaction import HTTPUserInteraction, InteractionStore
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.permissions_pipeline import PermissionsPipeline


class _FakeTool(BaseTool):
    name: str = "bash"
    description: str = "fake bash for permission contract"
    category: ToolCategory = ToolCategory.CODE
    risk_level: str = "high"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, params: dict, context=None) -> dict:
        return {"ok": True}


@pytest.mark.asyncio
async def test_ask_tool_suspends_then_allows_on_resolve() -> None:
    """真挂起闭环：bash（ask 名单）→ check 挂起 → 用户批准 → allow 同 turn 返回。"""
    store = InteractionStore()
    frames: list = []

    async def _on(item) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=_on)
    pp = PermissionsPipeline(ask_tools={"bash"}, user_interaction=ui)
    tc = ToolCall(id="t1", name="bash", arguments={"command": "echo hi && pwd"})

    task = asyncio.create_task(pp.check(tc, _FakeTool(), mode="execute"))
    await asyncio.sleep(0)
    assert task.done() is False, "用户决策前 check 必须挂起（真挂起语义）"
    assert len(frames) == 1 and frames[0].kind == "permission"
    assert frames[0].payload["tool_name"] == "bash"

    store.resolve(frames[0].interaction_id, True)
    verdict = await task
    assert verdict.allow is True


@pytest.mark.asyncio
async def test_ask_tool_timeout_denies_fail_closed() -> None:
    store = InteractionStore()
    frames: list = []

    async def _on(item) -> None:
        frames.append(item)

    # permission 挂起窗口已独立成 permission_ttl（默认 1800s）；fail-closed 契约注入短窗口
    ui = HTTPUserInteraction(
        store, session_id="s1", on_interaction=_on, wait_timeout=0.05, permission_ttl=0.05
    )
    pp = PermissionsPipeline(ask_tools={"bash"}, user_interaction=ui)
    tc = ToolCall(id="t1", name="bash", arguments={"command": "danger"})
    verdict = await pp.check(tc, _FakeTool(), mode="execute")
    assert verdict.allow is False
    assert (
        "user denied" in verdict.reason
        or "timeout" in verdict.reason.lower()
        or "denied" in verdict.reason
    )


@pytest.mark.asyncio
async def test_ask_tool_reject_denies() -> None:
    store = InteractionStore()
    frames: list = []

    async def _on(item) -> None:
        frames.append(item)

    ui = HTTPUserInteraction(store, session_id="s1", on_interaction=_on)
    pp = PermissionsPipeline(ask_tools={"bash"}, user_interaction=ui)
    tc = ToolCall(id="t1", name="bash", arguments={"command": "rm"})
    task = asyncio.create_task(pp.check(tc, _FakeTool(), mode="execute"))
    await asyncio.sleep(0)
    store.reject(frames[0].interaction_id)
    verdict = await task
    assert verdict.allow is False
    assert "user denied" in verdict.reason


@pytest.mark.asyncio
async def test_no_ui_still_denies_fail_closed() -> None:
    """未装配 UI（headless 兜底）→ 直接 deny，行为不回退。"""
    pp = PermissionsPipeline(ask_tools={"bash"})  # user_interaction=None
    tc = ToolCall(id="t1", name="bash", arguments={"command": "echo hi && pwd"})
    verdict = await pp.check(tc, _FakeTool(), mode="execute")
    assert verdict.allow is False
    assert "no confirmation channel" in verdict.reason
