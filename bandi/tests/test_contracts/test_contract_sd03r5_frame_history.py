# 功能：契约——completed 帧历史上限（滑动窗口：保留最近 N 条，防长驻 scope 无限增长）。
# 钉行为不钉实现：完成超量流程后 completed 恰保留最近 MAX_COMPLETED_FRAMES 条（最早淘汰）
# active/pending 不受裁剪影响（破坏者：只动 completed 区段）、上限常量可运行时配置。
from __future__ import annotations

from bandi.skills import runtime as runtime_module
from bandi.skills.card import SkillCard
from bandi.skills.runtime import SkillFrameStatus, SkillRuntime


def _card_two_nodes() -> SkillCard:
    """功能：契约基准卡——两节点线性 SOP（start → terminal）。"""
    return SkillCard(
        **{
            "skill_id": "sk_cap",
            "name": "上限用例",
            "version": "1.0.0",
            "description": "两节点流程。",
            "nodes": [
                {"node_id": "n1", "name": "起点", "type": "collect_info", "instruction": "开始。"},
                {"node_id": "n2", "name": "终点", "type": "response", "instruction": "结束。"},
            ],
            "edges": [{"source_node_id": "n1", "next_node_id": "n2", "priority": 0}],
            "start_node_id": "n1",
            "terminal_node_ids": ["n2"],
        }
    )


# completed 保留最近 N 条（早完成者先淘汰），非无界增长
def test_completed_history_keeps_most_recent_frames(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "MAX_COMPLETED_FRAMES", 3)
    runtime = SkillRuntime()
    card = _card_two_nodes()
    stack = runtime.new_stack("sess-cap")

    for index in range(5):
        stack = runtime.start(stack, card, task_id=f"t{index}")
        stack = runtime.complete(stack)

    assert [frame.task_id for frame in stack.completed] == ["t2", "t3", "t4"]


# 到达 terminal 自动闭环路径同样受上限约束（advance 路径非遗漏）
def test_advance_terminal_path_respects_cap(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "MAX_COMPLETED_FRAMES", 2)
    runtime = SkillRuntime()
    card = _card_two_nodes()
    stack = runtime.new_stack("sess-cap-terminal")

    for index in range(4):
        stack = runtime.start(stack, card, task_id=f"a{index}")
        stack = runtime.advance(stack, card)  # n1 → n2（terminal，自动入 completed）

    assert [frame.task_id for frame in stack.completed] == ["a2", "a3"]


# 破坏者：裁剪只动 completed 区段——active/pending 逐字不变
def test_trim_only_touches_completed_bucket(monkeypatch) -> None:
    monkeypatch.setattr(runtime_module, "MAX_COMPLETED_FRAMES", 1)
    runtime = SkillRuntime()
    card = _card_two_nodes()
    stack = runtime.new_stack("sess-cap-buckets")
    stack = runtime.start(stack, card, task_id="done-1")
    stack = runtime.complete(stack)
    stack = runtime.start(stack, card, task_id="suspended-1")
    stack = runtime.suspend(stack)
    stack = runtime.start(stack, card, task_id="active-1")
    stack = runtime.complete(stack)  # completed 现含 done-1 + active-1 → 裁到 1

    assert [frame.task_id for frame in stack.completed] == ["active-1"]
    assert [frame.task_id for frame in stack.pending] == ["suspended-1"]
    assert stack.active is None


# 上限未达时不裁剪（默认常量下 2 条完整保留）
def test_below_cap_keeps_all_frames() -> None:
    runtime = SkillRuntime()
    card = _card_two_nodes()
    stack = runtime.new_stack("sess-under-cap")
    for index in range(2):
        stack = runtime.start(stack, card, task_id=f"u{index}")
        stack = runtime.complete(stack)

    assert [frame.task_id for frame in stack.completed] == ["u0", "u1"]
    assert all(frame.status == SkillFrameStatus.COMPLETED for frame in stack.completed)
