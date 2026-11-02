# 功能：契约——Skill Card 任务帧栈（SkillRuntime 帧引擎 + frame_store 帧持久化）。
# 钉行为不钉实现：契约断言「最终产物」——帧内 node_id/status/slots 的逐字取值
# 歧义出边不猜条件而安全收束（帧逐字不变）、到达 terminal 自动闭环入 completed
# 挂起/恢复的栈式切换保持槽位逐字一致、越界 scope_id 不得写出目录（破坏者）
# 落盘读回等于内存栈（DoD-12）。
# 生产路径族：SkillRuntime（建帧/推进/挂起/恢复/完成 + 槽位水合 + awaiting 裁剪）
# + frame_store（save_frames/load_frames = 帧状态新生产消费路径）。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.skills.card import SkillCard
from bandi.skills.frame_store import (
    SkillFrameLoadError,
    SkillFrameSaveError,
    load_frames,
    save_frames,
)
from bandi.skills.runtime import (
    SkillFrameNotFoundError,
    SkillFrameStatus,
    SkillFrameTransitionError,
    SkillRuntime,
)


def _card_three_nodes() -> dict:
    """功能：契约基准卡——三节点线性 SOP（collect → confirm → reply terminal）。"""
    return {
        "skill_id": "sk_ship_order",
        "name": "发货 SOP",
        "version": "1.0.0",
        "description": "处理发货流程。",
        "required_info": ["订单号"],
        "nodes": [
            {
                "node_id": "collect_order",
                "name": "收集订单",
                "type": "collect_info",
                "instruction": "收集订单号。",
                "expected_user_info": ["订单号"],
            },
            {
                "node_id": "confirm_shipment",
                "name": "确认发货",
                "type": "notify",
                "instruction": "确认发货信息。",
                "expected_user_info": ["收货地址"],
            },
            {
                "node_id": "reply_final_result",
                "name": "反馈结果",
                "type": "response",
                "instruction": "给出最终回复。",
            },
        ],
        "edges": [
            {"source_node_id": "collect_order", "next_node_id": "confirm_shipment", "priority": 0},
            {
                "source_node_id": "confirm_shipment",
                "next_node_id": "reply_final_result",
                "priority": 0,
            },
        ],
        "start_node_id": "collect_order",
        "terminal_node_ids": ["reply_final_result"],
    }


def _card_ambiguous_edges() -> dict:
    """功能：破坏者基准卡——起点两条出边**全部带条件**（引擎不得猜条件，须安全收束）。"""
    data = _card_three_nodes()
    data["edges"] = [
        {
            "source_node_id": "collect_order",
            "next_node_id": "confirm_shipment",
            "condition": "订单类型 == 普通",
            "priority": 0,
        },
        {
            "source_node_id": "collect_order",
            "next_node_id": "reply_final_result",
            "condition": "订单类型 == 加急",
            "priority": 1,
        },
        {
            "source_node_id": "confirm_shipment",
            "next_node_id": "reply_final_result",
            "priority": 0,
        },
    ]
    return data


def _card_priority_edges() -> dict:
    """功能：破坏者基准卡——两条**无条件**出边，priority 0/5（须走 priority 大的分支）。"""
    data = _card_three_nodes()
    data["edges"] = [
        {"source_node_id": "collect_order", "next_node_id": "confirm_shipment", "priority": 0},
        {"source_node_id": "collect_order", "next_node_id": "reply_final_result", "priority": 5},
        {"source_node_id": "confirm_shipment", "next_node_id": "reply_final_result", "priority": 0},
    ]
    return data


def _card_with_user_name() -> dict:
    """功能：水合基准卡——required_info 额外声明 user_name（触发记忆 profile 补水合）。"""
    data = _card_three_nodes()
    data["required_info"] = ["订单号", "user_name"]
    return data


@pytest.fixture
def runtime(tmp_path: Path) -> SkillRuntime:
    return SkillRuntime(skills_dir=str(tmp_path / "skills"))


def _dump(frame) -> dict:
    """功能：帧逐字快照（排除随时间变化的 updated_at，用于「帧未被改动」断言）。"""
    return frame.model_dump(exclude={"updated_at"}, mode="json")


def test_r1_start_builds_active_frame_at_start_node(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.new_stack("sess-1")

    started = runtime.start(stack, card, task_id="t1", slots={"订单号": "A100"})

    assert started.scope_id == "sess-1"
    assert started.active is not None
    assert started.active.task_id == "t1"
    assert started.active.skill_id == "sk_ship_order"
    assert started.active.node_id == "collect_order"
    assert started.active.status == SkillFrameStatus.ACTIVE
    assert started.active.slots == {"订单号": "A100"}
    assert started.active.awaiting_fields == []
    assert started.pending == []
    assert started.completed == []


def test_r2_duplicate_task_id_is_idempotent(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.new_stack("sess-1")
    first = runtime.start(stack, card, task_id="t1")

    second = runtime.start(first, card, task_id="t1")

    assert _dump(second.active) == _dump(first.active)
    assert second.pending == []
    assert second.completed == []


def test_r3_advance_moves_to_next_node(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1")

    advanced = runtime.advance(stack, card)

    assert advanced.active is not None
    assert advanced.active.node_id == "confirm_shipment"
    assert advanced.active.status == SkillFrameStatus.ACTIVE
    assert advanced.completed == []
    assert advanced.active.awaiting_fields == ["订单号", "收货地址"]


def test_r4_advance_to_terminal_completes_frame(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1", slots={"订单号": "A100"})
    mid = runtime.advance(stack, card)

    finished = runtime.advance(mid, card)

    assert finished.active is None
    assert finished.pending == []
    assert len(finished.completed) == 1
    done = finished.completed[0]
    assert done.task_id == "t1"
    assert done.node_id == "reply_final_result"
    assert done.status == SkillFrameStatus.COMPLETED
    assert done.slots == {"订单号": "A100"}


def test_r5_conditional_only_edges_raise_without_mutation(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_ambiguous_edges())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1")
    before = _dump(stack.active)

    with pytest.raises(SkillFrameTransitionError):
        runtime.advance(stack, card)

    assert _dump(stack.active) == before
    assert stack.active.node_id == "collect_order"


def test_r6_explicit_illegal_next_node_raises_without_mutation(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1")
    before = _dump(stack.active)

    with pytest.raises(SkillFrameTransitionError):
        runtime.advance(stack, card, next_node_id="reply_final_result")

    assert _dump(stack.active) == before


def test_r7_priority_decides_among_unconditional_edges(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_priority_edges())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1")

    advanced = runtime.advance(stack, card)

    assert advanced.active is None
    assert len(advanced.completed) == 1
    assert advanced.completed[0].node_id == "reply_final_result"
    assert advanced.completed[0].status == SkillFrameStatus.COMPLETED


def test_r8_suspend_moves_active_frame_to_pending(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1", slots={"订单号": "A100"})
    before = stack.active.model_dump(exclude={"status", "updated_at"}, mode="json")

    suspended = runtime.suspend(stack)

    assert suspended.active is None
    assert len(suspended.pending) == 1
    frame = suspended.pending[0]
    assert frame.status == SkillFrameStatus.SUSPENDED
    assert frame.model_dump(exclude={"status", "updated_at"}, mode="json") == before


def test_r9_resume_switches_and_suspends_previous_active(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    base = runtime.new_stack("sess-1")
    with_a = runtime.start(base, card, task_id="t1")
    with_b = runtime.start(runtime.suspend(with_a), card, task_id="t2", slots={"订单号": "B200"})

    switched = runtime.resume(with_b, "t1")

    assert switched.active is not None
    assert switched.active.task_id == "t1"
    assert switched.active.status == SkillFrameStatus.ACTIVE
    assert [frame.task_id for frame in switched.pending] == ["t2"]
    assert switched.pending[0].status == SkillFrameStatus.SUSPENDED
    assert switched.pending[0].slots == {"订单号": "B200"}


def test_r10_resume_unknown_task_id_raises_without_mutation(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1")
    before_active = _dump(stack.active)

    with pytest.raises(SkillFrameNotFoundError):
        runtime.resume(stack, "ghost")

    assert _dump(stack.active) == before_active
    assert stack.pending == []


def test_r11_suspend_and_complete_without_active_raise(runtime: SkillRuntime) -> None:
    empty = runtime.new_stack("sess-1")

    with pytest.raises(SkillFrameTransitionError):
        runtime.suspend(empty)
    with pytest.raises(SkillFrameTransitionError):
        runtime.complete(empty)

    assert empty.active is None
    assert empty.completed == []


def test_r12_hydrate_slots_fills_missing_required_info(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_with_user_name())
    memory = [
        {"kind": "profile", "metadata": {"key": "preferred_name"}, "content": "老板"},
        {"kind": "note", "metadata": {"key": "preferred_name"}, "content": "忽略我"},
    ]

    patch = runtime.hydrate_slots(card, {"订单号": "A100"}, memory)

    assert patch == {"user_name": "老板"}


def test_r13_hydrate_slots_never_overwrites_existing_value(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_with_user_name())
    memory = [{"kind": "profile", "metadata": {"key": "preferred_name"}, "content": "老板"}]

    patch = runtime.hydrate_slots(card, {"user_name": "老王"}, memory)

    assert patch == {}


def test_r14_awaiting_fields_trim_satisfied(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())

    assert runtime.awaiting_fields(card, {}, node_id="collect_order") == ["订单号"]
    assert runtime.awaiting_fields(card, {}, node_id="confirm_shipment") == ["订单号", "收货地址"]
    assert runtime.awaiting_fields(card, {"订单号": "A100"}, "confirm_shipment") == ["收货地址"]
    assert (
        runtime.awaiting_fields(card, {"订单号": "A100", "收货地址": "北京"}, "confirm_shipment")
        == []
    )


def test_r15_frame_roundtrip_is_verbatim(runtime: SkillRuntime, tmp_path: Path) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(
        runtime.new_stack("会话-中文"), card, task_id="t1", slots={"订单号": "A100", "备注": "加急"}
    )
    suspended = runtime.suspend(stack)
    resumed = runtime.start(suspended, card, task_id="t2")

    saved_path = save_frames(resumed, str(tmp_path / "frames"))
    reloaded = load_frames("会话-中文", str(tmp_path / "frames"))

    assert saved_path.exists()
    assert reloaded is not None
    assert reloaded.model_dump(mode="json") == resumed.model_dump(mode="json")


def test_r16_scope_id_traversal_and_corrupt_file_fail_loudly(tmp_path: Path) -> None:
    card = SkillCard(**_card_three_nodes())
    runtime = SkillRuntime(skills_dir=str(tmp_path / "skills"))
    stack = runtime.start(runtime.new_stack("../evil"), card, task_id="t1")

    with pytest.raises(SkillFrameSaveError):
        save_frames(stack, str(tmp_path / "frames"))

    assert load_frames("never-written", str(tmp_path / "frames")) is None

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    (frames_dir / "broken.frames.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(SkillFrameLoadError):
        load_frames("broken", str(frames_dir))

    assert not (tmp_path / "evil.json").exists()


def test_r17_mismatched_card_skill_id_is_rejected(runtime: SkillRuntime) -> None:
    card = SkillCard(**_card_three_nodes())
    stack = runtime.start(runtime.new_stack("sess-1"), card, task_id="t1")
    other_flow = SkillCard(**{**_card_three_nodes(), "skill_id": "sk_other_flow"})
    before = _dump(stack.active)

    with pytest.raises(SkillFrameTransitionError):
        runtime.advance(stack, other_flow)

    assert _dump(stack.active) == before
    assert stack.active.node_id == "collect_order"
