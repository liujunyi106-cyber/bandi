# 功能：VER 验证闸门契约测试——工具结果物化后验证不得被绕过（行为族钉）
# 破坏者视角：工具轮执行后 tool_results 被 on_thinking 清空（已物化为 TOOL 消息）
# 最终回答轮 REFLECTING 若只看瞬时 tool_results 会判定"无工具、不验证"——
# 工具→综合多轮场景下 VER 空转。契约：验证触发必须基于持久工具证据（消息窗口）。

import pytest

from bandi.agent.base import ExecutionContext
from bandi.agent.verifier import Verifier, VerifierPolicy
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import Message, MessageRole
from bandi.llm.mock import MockProvider


@pytest.fixture
def ctx() -> ExecutionContext:
    return ExecutionContext(
        messages=[],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
    )


def _tool_msg(name: str, content: str = "done") -> Message:
    return Message(
        role=MessageRole.TOOL,
        content=f'<tool_output tool="{name}">{content}</tool_output>',
        tool_call_id="tc-1",
        name=name,
        turn_index=1,
    )


def test_bypass_scenario_tool_results_cleared_still_requires_verify(ctx: ExecutionContext) -> None:
    """绕过场景：tool_results 已清空（on_thinking 物化后），messages 尾部含
    写类工具结果——最终综合轮必须仍要求验证。"""
    ctx.messages = [
        Message(role=MessageRole.USER, content="创建文件"),
        Message(role=MessageRole.ASSISTANT, content="", turn_index=1),
        _tool_msg("write"),
        Message(role=MessageRole.ASSISTANT, content="已创建", turn_index=2),
    ]
    ctx.tool_results = []
    v = Verifier()
    assert v.verification_required(ctx) is True, "工具结果在消息尾部时不得绕过 VER"


def test_plain_chat_never_requires_verify(ctx: ExecutionContext) -> None:
    """纯对话轮（无任何工具证据）不要求验证——多轮聊天不受打扰。"""
    ctx.messages = [
        Message(role=MessageRole.USER, content="你好"),
        Message(role=MessageRole.ASSISTANT, content="你好", turn_index=1),
    ]
    ctx.tool_results = []
    v = Verifier()
    assert v.verification_required(ctx) is False


def test_readonly_tool_messages_not_code_edit_trigger(ctx: ExecutionContext) -> None:
    """消息窗口内只读工具结果不触发代码编辑类验证（require_on_code_edits）。"""
    ctx.messages = [
        Message(role=MessageRole.USER, content="搜索资料"),
        Message(role=MessageRole.ASSISTANT, content="", turn_index=1),
        _tool_msg("web_search"),
        Message(role=MessageRole.ASSISTANT, content="结果", turn_index=2),
    ]
    ctx.tool_results = []
    v = Verifier()
    assert v.verification_required(ctx) is False, "只读工具 + 数量不足不得触发 VER"


def test_tool_messages_outside_window_do_not_trigger(ctx: ExecutionContext) -> None:
    """破坏者视角：工具证据滑出尾部窗口（先多轮聊天后轮不含工具）
    —— 历史工具轮不得对当前纯聊天轮重复触发验证。"""
    v = Verifier(policy=VerifierPolicy(require_on_code_edits=True, min_todos_for_verification=2))
    ctx.tool_results = []
    messages = [
        _tool_msg("write"),
        Message(role=MessageRole.ASSISTANT, content="旧", turn_index=1),
    ]
    for i in range(8):
        messages.append(Message(role=MessageRole.USER, content=f"u{i}", turn_index=i + 2))
        messages.append(Message(role=MessageRole.ASSISTANT, content=f"a{i}", turn_index=i + 2))
    ctx.messages = messages
    assert v.verification_required(ctx) is False, "窗口外工具证据不得误触发"


def test_disabled_policy_never_verifies(ctx: ExecutionContext) -> None:
    ctx.messages = [_tool_msg("write")]
    v = Verifier(policy=VerifierPolicy(enabled=False))
    assert v.verification_required(ctx) is False


def test_quantity_trigger_counts_message_evidence(ctx: ExecutionContext) -> None:
    """数量阈值触发：消息窗口内工具结果数 ≥ min_todos 即要求验证（与瞬时列表一次）。"""
    v = Verifier(policy=VerifierPolicy(require_on_code_edits=False, min_todos_for_verification=2))
    ctx.tool_results = []
    ctx.messages = [
        Message(role=MessageRole.USER, content="批量处理"),
        Message(role=MessageRole.ASSISTANT, content="", turn_index=1),
        _tool_msg("search_a"),
        _tool_msg("search_b"),
        Message(role=MessageRole.ASSISTANT, content="完成", turn_index=2),
    ]
    assert v.verification_required(ctx) is True, "消息窗口工具数达标必须触发 VER"


# ── 生产主链路端到端：工具→综合轮 VER 不得被绕过（功能验证） ────────────


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


class _Exec:
    async def execute(self, tool_calls: list[object]) -> list[object]:
        from bandi.core.message import ToolResult

        return [
            ToolResult(tool_call_id=tc.id, tool_name=tc.name, success=True, output="ok")
            for tc in tool_calls
        ]


@pytest.mark.asyncio
async def test_e2e_tool_then_final_answer_still_verifies() -> None:
    """端到端（生产主链路）：工具执行轮 → 综合回答轮，VER 闸门必须真实执行
    （VERDICT_ISSUED 事件发出）——修复前 on_thinking 清空 tool_results 后
    verification_required 短路成 VERIFIED，judge 永不触发。"""
    from bandi.agent.loop import AgentLoop
    from bandi.core.events import EventType
    from bandi.core.message import LLMResponse, ToolCall

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="c1", name="write", arguments={"path": "a.txt"})],
        )
    )
    provider.add_response(LLMResponse(content="已创建 a.txt"))
    bus = _RecordingBus()
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="创建文件 a.txt")],
        provider=provider,
        config=AgentConfig(),
        bus=bus,
    )
    ctx.tool_executor = _Exec()
    ctx.verifier = Verifier()
    from types import SimpleNamespace

    ctx.safety = SimpleNamespace(
        verifier=ctx.verifier,
        prompt_guard=None,
        content_filter=None,
        pii_detector=None,
        output_sanitizer=None,
    )

    loop = AgentLoop()
    await loop.run(ctx)

    verdicts = [e for e in bus.events if getattr(e, "type", None) == EventType.VERDICT_ISSUED]
    assert len(verdicts) == 1, (
        f"工具→综合轮必须发出 VERDICT_ISSUED（实际 {len(verdicts)}）——VER 闸门被绕过"
    )
    assert ctx.terminal_reason is not None, "验证后必须到达终止态"
