"""
  常驻任务状态块：五槽（当前目标 / 上轮结论 / 未完成项 / 本轮原话 / 活跃产物）
  注入位置＝请求视图最后一轮 user 之前（不写回历史、不累积、不打前缀缓存）。

破坏者断言：
- 重复注入同一视图不得叠加两份状态块（幂等替换）；
- 状态块为空串时视图与输入等价（不得凭空多出消息）；
- 注入不得就地修改入参 Message（历史原对象保持不可变）。
"""

from typing import Any

from bandi.core.message import Message, MessageRole


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


# ── 状态块渲染 ────────────────────────────────────────────────────────────


def test_state_block_renders_five_slots() -> None:
    from bandi.memory.context_projection import build_state_block

    block = build_state_block(
        goal="做一个待办事项 App",
        outcome="done",
        pending=["加筛选", "删除功能"],
        raw_input="接着把标签补上",
        artifacts=["canvas/todo/index.html"],
    )
    assert block.startswith("[任务状态]")
    assert "当前目标: 做一个待办事项 App" in block
    assert "上轮结论: done" in block
    assert "未完成: 2 项（加筛选；删除功能）" in block
    assert "本轮原话: 接着把标签补上" in block
    assert "活跃产物: canvas/todo/index.html" in block
    assert "\r" not in block


def test_state_block_renders_original_goal_before_current_goal() -> None:
    from bandi.memory.context_projection import build_state_block

    block = build_state_block(
        original_goal="最初目标：完成竞品分析",
        goal="当前子任务：改表格颜色",
    )
    assert "原始目标: 最初目标：完成竞品分析" in block
    assert block.index("原始目标:") < block.index("当前目标:")


def test_state_block_omits_empty_slots() -> None:
    from bandi.memory.context_projection import build_state_block

    block = build_state_block(
        goal="排查 seed 报错",
        outcome="partial",
        pending=[],
        raw_input="继续",
        artifacts=[],
    )
    assert "未完成" not in block
    assert "活跃产物" not in block
    assert "上轮结论: partial" in block


def test_collect_uses_latest_earlier_snapshot(tmp_path: Any) -> None:
    """形态：快照存在断档（turn:2 之后几轮未写入），状态块仍须取最近一条更早快照。"""
    from bandi.memory.context_projection import collect_state_block
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "w.db"))
    store.set("turn:2", "目标: A | 终态: done | 产物: /p/a.html", session_id="s1")

    block = collect_state_block(working=store, session_id="s1", turn_index=7, raw_input="")

    assert "上轮结论: done" in block
    assert "/p/a.html" in block


def test_collect_returns_empty_without_any_source() -> None:
    from bandi.memory.context_projection import collect_state_block

    assert collect_state_block(turn_index=3, raw_input="你好") == ""


def test_state_block_renders_hint_slot() -> None:
    from bandi.memory.context_projection import build_state_block

    block = build_state_block(goal="做博客", hint="本轮可能是新任务")
    assert "提示: 本轮可能是新任务" in block


def test_switch_hint_fires_on_explicit_switch() -> None:
    from bandi.memory.context_projection import detect_task_switch_hint

    assert detect_task_switch_hint("先不弄这个了，帮我搭个个人博客") != ""
    assert detect_task_switch_hint("换个方向，写个网页版的简历") != ""


def test_switch_hint_silent_on_continuation() -> None:
    from bandi.memory.context_projection import detect_task_switch_hint

    assert detect_task_switch_hint("继续把删除功能加上") == ""
    assert detect_task_switch_hint("另外把这个加上") == "", "带指代的『另外』属续做，不得提示"


def test_collect_includes_hint_even_without_other_sources() -> None:
    from bandi.memory.context_projection import collect_state_block

    block = collect_state_block(turn_index=3, raw_input="先停一下，我有个新需求：做个预约系统")
    assert "提示:" in block, "显式换任务提示不依赖台账/快照"


def test_collect_hint_survives_blank_raw_slot() -> None:
    """形态：「本轮原话」槽因去重被置空，换任务判定仍须用原文（hint_source）。"""
    from bandi.memory.context_projection import collect_state_block

    block = collect_state_block(
        turn_index=3,
        raw_input="",
        hint_source="先停一下，我有个新需求：做个预约系统",
    )
    assert "提示:" in block
    assert "本轮原话" not in block


def test_state_block_is_length_bounded() -> None:
    from bandi.memory.context_projection import build_state_block

    block = build_state_block(
        goal="目" * 2000,
        outcome="blocked",
        pending=["待" * 500, "办" * 500],
        raw_input="问" * 2000,
        artifacts=["/p/" + "x" * 500],
    )
    assert len(block) <= 900


# ── 视图注入 ──────────────────────────────────────────────────────────────


def test_inject_attaches_to_last_user_only() -> None:
    from bandi.agent.request_view import inject_state_block

    history = [_user("第一轮"), _assistant("好的"), _user("第二轮")]
    view = inject_state_block(history, "[任务状态]\n当前目标: X")

    assert view is not history, "必须返回新列表"
    assert view[0].content == "第一轮", "历史消息不得改写"
    assert view[1].content == "好的"
    assert view[2].content.endswith("第二轮")
    assert "[任务状态]" in view[2].content
    assert view[2].content.index("[任务状态]") < view[2].content.index("第二轮")
    assert len(view) == 3, "不得追加新消息"


def test_inject_is_idempotent() -> None:
    from bandi.agent.request_view import inject_state_block

    once = inject_state_block([_user("第二轮")], "[任务状态]\n当前目标: X")
    twice = inject_state_block(list(once), "[任务状态]\n当前目标: Y")

    assert twice[-1].content.count("[任务状态]") == 1
    assert "当前目标: Y" in twice[-1].content
    assert "当前目标: X" not in twice[-1].content


def test_inject_noop_on_empty_block() -> None:
    from bandi.agent.request_view import inject_state_block

    history = [_user("第一轮")]
    view = inject_state_block(history, "")
    assert view[0].content == "第一轮"


def test_inject_does_not_mutate_input_message() -> None:
    from bandi.agent.request_view import inject_state_block

    original = _user("第二轮")
    inject_state_block([original], "[任务状态]\n当前目标: X")
    assert original.content == "第二轮", "入参 Message 必须保持不可变"


def test_inject_appends_when_no_user_message() -> None:
    from bandi.agent.request_view import inject_state_block

    view = inject_state_block([_assistant("只有助手消息")], "[任务状态]\n当前目标: X")
    assert view[-1].role == MessageRole.USER
    assert "[任务状态]" in (view[-1].content or "")
