"""契约：连续性锚点改「头+尾」；审批锚点由执行链直传调度侧。

- 锚点：超长 raw 只回注头 600 + 尾 200（+ 中段省略注记），最后的要求不被砍掉。
- 审批：states 落 meta → AgentTurnResult → run_agent 回填 capture → SeatResult metadata
  → 卡上 `_approval_tool_call_id`；调度侧优先用执行链上报值，缺失才回查 pending。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.states import (
    _ANCHOR_RAW_MAX_CHARS,
    _ANCHOR_RAW_TAIL_CHARS,
    _inject_continuity_anchor,
)
from bandi.company.company_mode import _approval_ref_for_result
from bandi.company.seat_executor import SeatResult, approval_metadata
from bandi.core.agent_runner import AgentTurnResult
from bandi.core.message import Message, MessageRole


def _ctx(raw: str) -> Any:
    from bandi.agent.base import CompressionState

    return SimpleNamespace(
        raw_input=raw,
        messages=[
            Message(role=MessageRole.USER, content="历史已压缩为摘要"),
            Message(role=MessageRole.ASSISTANT, content="收尾"),
        ],
        compress=CompressionState(),
    )


def test_anchor_keeps_head_and_tail() -> None:
    raw = "记住暗号是 pineapple-77。" + "填充材料。" * 400 + "最后要求：输出三条结论。"
    ctx = _ctx(raw)

    _inject_continuity_anchor(ctx)

    anchors = [m.content or "" for m in ctx.messages if m.meta.get("synthetic") is False]
    assert len(anchors) == 1, "raw 被裁后必须注入任务锚点"
    content = anchors[0]
    assert content.startswith("记住暗号是 pineapple-77"), "头段任务关键不得丢"
    assert content.endswith("最后要求：输出三条结论。"), "尾段最后要求必须保留"
    assert "原始输入中段省略" in content, "中段省略必须显式标注"
    assert len(content) <= _ANCHOR_RAW_MAX_CHARS + _ANCHOR_RAW_TAIL_CHARS + 80, (
        f"锚点必须有界（头+尾），实际 {len(content)}"
    )


def test_anchor_short_input_untouched() -> None:
    ctx = _ctx("短指令")

    _inject_continuity_anchor(ctx)

    anchors = [m.content or "" for m in ctx.messages if m.meta.get("synthetic") is False]
    assert anchors == ["短指令"], "短输入原样回注，不加省略标记"


def test_agent_turn_result_defaults_have_no_approval() -> None:
    result = AgentTurnResult(
        reply="", terminal_reason=None, total_tokens=0, turn_index=0, session_id="s"
    )
    assert result.approval_tool_call_id == ""
    assert result.approval_tool_name == ""


def test_run_agent_fills_approval_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.agent import factory

    async def _fake_turn(**_kwargs: Any) -> AgentTurnResult:
        return AgentTurnResult(
            reply="等待审批",
            terminal_reason="AWAITING_APPROVAL",
            total_tokens=12,
            turn_index=1,
            session_id="s1",
            approval_tool_call_id="call-42",
            approval_tool_name="bash",
        )

    monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", _fake_turn)
    capture: dict[str, Any] = {}

    import asyncio

    asyncio.run(factory.run_agent(user_input="hi", approval_capture=capture))

    assert capture == {"tool_call_id": "call-42", "tool_name": "bash"}


def test_run_agent_without_capture_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from bandi.agent import factory

    async def _fake_turn(**_kwargs: Any) -> AgentTurnResult:
        return AgentTurnResult(
            reply="ok", terminal_reason="COMPLETED", total_tokens=1, turn_index=0, session_id="s"
        )

    monkeypatch.setattr("bandi.core.agent_runner.run_agent_turn", _fake_turn)

    import asyncio

    out = asyncio.run(factory.run_agent(user_input="hi"))
    assert out[0] == "ok"


def test_seat_approval_metadata_shape() -> None:
    assert approval_metadata(None) == {}
    assert approval_metadata({}) == {}
    assert approval_metadata({"tool_call_id": "", "tool_name": ""}) == {}
    assert approval_metadata({"tool_call_id": "c1"}) == {"approval_tool_call_id": "c1"}
    assert approval_metadata({"tool_call_id": "c1", "tool_name": "bash"}) == {
        "approval_tool_call_id": "c1",
        "approval_tool_name": "bash",
    }


def test_card_approval_ref_prefers_executor_record(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SeatResult(
        success=True,
        output="等待审批",
        metadata={
            "terminal_reason": "AWAITING_APPROVAL",
            "approval_tool_call_id": "call-exec",
            "approval_tool_name": "bash",
        },
    )

    def _must_not_lookup(_sid: str) -> tuple[str, str] | None:
        raise AssertionError("执行链已上报锚点时不得再回查 pending")

    monkeypatch.setattr("bandi.company.company_mode._latest_pending_ref", _must_not_lookup)
    assert _approval_ref_for_result(result, "s1") == ("call-exec", "bash")


def test_card_approval_ref_falls_back_to_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SeatResult(success=True, output="", metadata={"terminal_reason": "AWAITING_APPROVAL"})
    monkeypatch.setattr(
        "bandi.company.company_mode._latest_pending_ref", lambda _sid: ("call-pending", "write")
    )

    assert _approval_ref_for_result(result, "s1") == ("call-pending", "write")


def test_card_approval_ref_without_any_record() -> None:
    result = SeatResult(success=True, output="", metadata={"terminal_reason": "AWAITING_APPROVAL"})
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("bandi.company.company_mode._latest_pending_ref", lambda _sid: None)
    try:
        assert _approval_ref_for_result(result, "s1") == ("", "")
    finally:
        monkeypatch.undo()
