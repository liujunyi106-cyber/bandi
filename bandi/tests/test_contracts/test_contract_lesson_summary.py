# 功能：契约——失败教训摘要化（教训不得携带
# 任务描述原文（跨项目/员工注入的信息面）；单条紧凑长度上界防写满 4000 字全局层）。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bandi.agent.states import _record_lesson
from bandi.agent.verifier import Verdict


class RecordingHierarchy:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, dict[str, Any]]] = []

    def save_memory(self, scope: str, content: str, **kwargs: Any) -> None:
        self.saved.append((scope, content, kwargs))


def _ctx(
    hierarchy: RecordingHierarchy,
    *,
    failed_tools: tuple[str, ...] = ("bash",),
    turn: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        meta={
            "hierarchy_manager": hierarchy,
            "session_id": "s1",
            "observing_digest": {"failed_tools": list(failed_tools)},
        },
        turn_index=turn,
    )


def test_lesson_excludes_task_description() -> None:
    """契约：教训内容不得携带任务描述原文——跨项目/跨员工注入时
    不得暴露具体任务细节（红：现状模板 `任务：{task_desc}` 原样写入）。"""
    h = RecordingHierarchy()
    verdict = Verdict(action="rework", reason="命令未返回预期输出")
    _record_lesson(_ctx(h), "机密任务PROJECT_X_DETAIL：露营灯市场调研", verdict)
    assert h.saved, "rework 必须写入教训"
    scope, lesson, kwargs = h.saved[0]
    assert scope == "global"
    assert "PROJECT_X_DETAIL" not in lesson, f"教训不得含任务描述原文：{lesson}"
    assert "露营灯" not in lesson
    assert kwargs.get("allow_shared") is True


def test_lesson_has_compact_upper_bound() -> None:
    """契约：单条教训长度紧凑（≤200 字）——4000 字全局层不得被
    少数几条写满（红：现状最多 400 字/条，10 条即满）。"""
    h = RecordingHierarchy()
    verdict = Verdict(action="fail", reason="原因" * 500)
    _record_lesson(
        _ctx(h, failed_tools=tuple(f"tool{i}" for i in range(30))), "任务" * 500, verdict
    )
    assert h.saved
    _, lesson, _ = h.saved[0]
    assert len(lesson) <= 200, f"单条教训超上界：{len(lesson)}"


def test_lesson_keeps_action_tools_reason() -> None:
    """契约（对照）：摘要化后核心信息保留——失败类型、失败工具、原因摘要。"""
    h = RecordingHierarchy()
    verdict = Verdict(action="rework", reason="工具参数格式错误导致校验失败")
    _record_lesson(_ctx(h, failed_tools=("bash", "python_exec")), "任意任务", verdict)
    _, lesson, _ = h.saved[0]
    assert "rework" in lesson
    assert "bash" in lesson and "python_exec" in lesson
    assert "工具参数格式错误" in lesson
