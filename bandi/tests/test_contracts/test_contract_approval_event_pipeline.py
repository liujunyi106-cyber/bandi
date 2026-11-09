# 功能：审批生产链契约——PolicyEngine ASK 路由 + executor 侧 APPROVAL_* 事件（6.2/8.2 补钉）
# （静默失败、不生成审批卡片）；8.2：审批请求/授权/拒绝无事件（trace 无法复盘）。
# 本契约钉三条生产路径：pipeline ask 事件 / policy ask 路由与事件 / auto_review 两态事件。
from __future__ import annotations

from typing import Any

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall
from bandi.core.permission.policy_engine import PolicyEngine
from bandi.core.permission.roles import register_builtin_role
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.permissions_pipeline import PermissionsPipeline
from bandi.tools.registry import ToolRegistry


class _BashStub(BaseTool):
    name: str = "bash"
    description: str = "stub"
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    risk_level: str = "high"

    async def execute(self, params: dict[str, Any], context: Any = None) -> dict[str, Any]:
        return {"ok": True, "command": params.get("command")}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_instance(_BashStub())
    return registry


async def _spy(bus: EventBus, *types: EventType) -> list[Event]:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    for event_type in types:
        await bus.subscribe(event_type, handler)
    return received


@pytest.fixture(autouse=True)
def _clean_approval_store():
    from bandi.tools.approval import reset_approval_store

    reset_approval_store()
    yield
    reset_approval_store()


class TestPipelineAskEvent:
    @pytest.mark.asyncio
    async def test_pipeline_ask_emits_requested_and_pends(self) -> None:
        """权限管道 ask → APPROVAL_REQUESTED（decided_by=pipeline）+ pending 落库。"""
        from bandi.tools.approval import APPROVAL_REQUIRED_PREFIX, get_approval_store

        pp = PermissionsPipeline(ask_tools={"bash"})
        bus = EventBus()
        events = await _spy(bus, EventType.APPROVAL_REQUESTED)
        executor = ToolExecutor(_registry(), event_bus=bus, permissions_pipeline=pp)
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})]
        )
        result = results[0]
        assert result.success is False
        assert APPROVAL_REQUIRED_PREFIX in (result.error or "")
        assert len(events) == 1
        assert events[0].data["decided_by"] == "pipeline"
        assert events[0].data["tool_name"] == "bash"
        assert get_approval_store().get_pending("default") is not None


class TestPolicyAskRouting:
    @pytest.mark.asyncio
    async def test_policy_ask_enqueues_pending_and_emits_event(self) -> None:
        """破坏者：角色策略 ASK 不得再被当 DENY——必须落 pending + 返回 APPROVAL_REQUIRED。"""
        from bandi.tools.approval import APPROVAL_REQUIRED_PREFIX, get_approval_store

        # bypass bash 走管道放行，确保命中 PolicyEngine 的角色 ASK 分支
        pp = PermissionsPipeline(ask_tools=set(), bypass_tools={"bash"})
        engine = PolicyEngine()
        assert register_builtin_role(engine, "power_user")
        bus = EventBus()
        events = await _spy(bus, EventType.APPROVAL_REQUESTED)
        executor = ToolExecutor(
            _registry(),
            event_bus=bus,
            permissions_pipeline=pp,
            policy_engine=engine,
            agent_role="power_user",
        )
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})]
        )
        result = results[0]
        assert result.success is False, "ASK 是待审批不是放行"
        assert APPROVAL_REQUIRED_PREFIX in (result.error or ""), (
            "ASK 必须返回审批卡片协议（此前被当 DENY 直接失败）"
        )
        assert len(events) == 1
        assert events[0].data["decided_by"] == "policy_ask"
        assert get_approval_store().get_pending("default") is not None


class TestApprovalClosureHeadless:
    @pytest.mark.asyncio
    async def test_approved_reissue_executes_pipeline_ask(self) -> None:
        """闭环：headless 批准落 store 后 re-issue 必须真实执行——
        pp.check 的 ask 层无通道二次拒绝会使审批形同虚设（放行也跑不了）。"""
        from bandi.tools.approval import get_approval_store, params_digest

        pp = PermissionsPipeline(ask_tools={"bash"})
        executor = ToolExecutor(_registry(), permissions_pipeline=pp)
        args = {"command": "echo hi"}
        first = (await executor.execute([ToolCall(id="c1", name="bash", arguments=dict(args))]))[0]
        assert first.success is False, "首击必须落审批（APPROVAL_REQUIRED）"

        get_approval_store().grant(
            "default", "bash", scope="once", params_digest=params_digest(args)
        )
        second = (await executor.execute([ToolCall(id="c2", name="bash", arguments=dict(args))]))[0]
        assert second.success is True, f"批准后 re-issue 必须执行（实际 {second.error}）"

    @pytest.mark.asyncio
    async def test_approved_reissue_executes_policy_ask(self) -> None:
        """闭环：角色策略 ASK 批准后 re-issue 不得重入审批（granted 旁路）。"""
        from bandi.tools.approval import get_approval_store

        pp = PermissionsPipeline(ask_tools=set(), bypass_tools={"bash"})
        engine = PolicyEngine()
        assert register_builtin_role(engine, "power_user")
        executor = ToolExecutor(
            _registry(),
            permissions_pipeline=pp,
            policy_engine=engine,
            agent_role="power_user",
        )
        args = {"command": "echo hi"}
        first = (await executor.execute([ToolCall(id="c1", name="bash", arguments=dict(args))]))[0]
        assert first.success is False and "APPROVAL_REQUIRED" in (first.error or "")

        store = get_approval_store()
        pending = store.get_pending("default")
        assert pending is not None
        store.grant("default", "bash", scope="once", params_digest=pending.params_digest)
        second = (await executor.execute([ToolCall(id="c2", name="bash", arguments=dict(args))]))[0]
        assert second.success is True, f"批准后 re-issue 必须执行（实际 {second.error}）"


class TestAutoReviewEvents:
    @pytest.mark.asyncio
    async def test_auto_review_allow_emits_granted(self) -> None:
        """auto_review 放行 → APPROVAL_GRANTED（decided_by=auto_review）+ 工具真执行。"""
        from bandi.tools.approval import get_approval_store

        async def fake_llm(messages: list[dict]) -> str:
            return '{"decision": "allow", "rationale": "safe read-only"}'

        pp = PermissionsPipeline(ask_tools={"bash"})
        bus = EventBus()
        granted = await _spy(bus, EventType.APPROVAL_GRANTED)
        executor = ToolExecutor(
            _registry(), event_bus=bus, permissions_pipeline=pp, auto_review_llm=fake_llm
        )
        get_approval_store().set_reviewer("default", "auto_review")
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})]
        )
        assert results[0].success is True, "机审放行后工具必须真实执行"
        assert len(granted) == 1
        assert granted[0].data["decided_by"] == "auto_review"

    @pytest.mark.asyncio
    async def test_auto_review_deny_emits_denied(self) -> None:
        """auto_review 拒绝 → APPROVAL_DENIED 事件 + 工具不执行。"""
        from bandi.tools.approval import get_approval_store

        async def fake_llm(messages: list[dict]) -> str:
            return '{"decision": "deny", "rationale": "destructive"}'

        pp = PermissionsPipeline(ask_tools={"bash"})
        bus = EventBus()
        denied = await _spy(bus, EventType.APPROVAL_DENIED)
        executor = ToolExecutor(
            _registry(), event_bus=bus, permissions_pipeline=pp, auto_review_llm=fake_llm
        )
        get_approval_store().set_reviewer("default", "auto_review")
        results = await executor.execute(
            [ToolCall(id="c1", name="bash", arguments={"command": "echo hi"})]
        )
        assert results[0].success is False
        assert len(denied) == 1
        assert denied[0].data["decided_by"] == "auto_review"
