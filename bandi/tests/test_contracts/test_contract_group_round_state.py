# 功能：契约——群聊 Round 纯状态模型（slot 顺序、seen 游标、上下文渲染、持久化往返）。
from __future__ import annotations

from bandi.company.group_chat_protocol import GROUP_PASS_HINT
from bandi.company.group_round import (
    GroupRound,
    RoundSlot,
    SlotOrigin,
    SlotStatus,
    default_relay_limit,
    render_round_context,
)


def _round() -> GroupRound:
    round_ = GroupRound(
        round_id="r1",
        session_id="s1",
        project_id="p1",
        user_input="做一版方案",
        sender_role="user",
        member_role_ids=["lead", "worker"],
    )
    round_.slots = [
        RoundSlot(slot_id="a", role_id="lead", order=0),
        RoundSlot(slot_id="b", role_id="worker", order=1),
    ]
    return round_


def test_add_line_assigns_monotonic_seq_and_seen_cursor() -> None:
    round_ = _round()
    first = round_.add_line("user", "user", "做一版方案")
    second = round_.add_line("lead", "bot", "我先看需求")
    assert (first.seq, second.seq) == (1, 2)
    assert round_.mark_seen("worker") == 2
    assert round_.seen["worker"] == 2


def test_unseen_lines_uses_seen_cursor_and_skips_own_lines() -> None:
    round_ = _round()
    round_.add_line("user", "user", "做一版方案")
    round_.add_line("lead", "bot", "我先看需求")
    round_.add_line("worker", "bot", "我在做实现")
    assert [line.text for line in round_.unseen_lines("worker")] == ["做一版方案", "我先看需求"]
    assert [line.text for line in round_.unseen_lines("lead")] == ["做一版方案", "我在做实现"]


def test_render_round_context_includes_order_previous_and_pass_rule() -> None:
    round_ = _round()
    round_.add_line("user", "user", "做一版方案")
    round_.add_line("lead", "bot", "我先看需求")
    round_.slots[0].status = SlotStatus.POSTED
    round_.slots[1].status = SlotStatus.SPEAKING
    text = render_round_context(
        round_,
        round_.slots[1],
        role_names={"lead": "负责人", "worker": "执行"},
    )
    assert "第 2 位" in text
    assert "原始目标：做一版方案" in text
    assert "负责人" in text
    assert "做一版方案" in text
    assert "我先看需求" in text
    assert GROUP_PASS_HINT.split("；")[0] in text


def test_default_relay_limit_is_three_times_members() -> None:
    assert default_relay_limit(0) == 1
    assert default_relay_limit(3) == 9


def test_round_roundtrip_preserves_slots_seen_and_status() -> None:
    round_ = _round()
    round_.add_line("user", "user", "做一版方案")
    round_.slots[0].status = SlotStatus.POSTED
    round_.slots[1].origin = SlotOrigin.BOT
    round_.slots[1].called_by = "lead"
    round_.seen["worker"] = 1
    round_.relay_count = 2
    restored = GroupRound.from_dict(round_.to_dict())
    assert restored.round_id == "r1"
    assert [slot.role_id for slot in restored.slots] == ["lead", "worker"]
    assert restored.slots[0].status == SlotStatus.POSTED
    assert restored.slots[1].origin == SlotOrigin.BOT
    assert restored.slots[1].called_by == "lead"
    assert restored.seen == {"worker": 1}
    assert restored.relay_count == 2


def test_next_waiting_slot_skips_non_waiting() -> None:
    round_ = _round()
    round_.slots[0].status = SlotStatus.POSTED
    assert round_.next_waiting_slot is round_.slots[1]
    round_.slots[1].status = SlotStatus.PASSED
    assert round_.next_waiting_slot is None
