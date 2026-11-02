# 功能：契约——全局失败教训按团队过滤注入 + 教训条目带团队标签（第二步
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bandi.agent.states import _record_lesson
from bandi.agent.verifier import Verdict


def _hierarchy(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


class RecordingHierarchy:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, dict[str, Any]]] = []

    def save_memory(self, scope: str, content: str, **kwargs: Any) -> None:
        self.saved.append((scope, content, kwargs))


def _ctx(hierarchy: RecordingHierarchy, *, team: str = "t1") -> SimpleNamespace:
    meta: dict[str, Any] = {
        "hierarchy_manager": hierarchy,
        "session_id": "s1",
        "observing_digest": {"failed_tools": ["bash"]},
    }
    if team:
        meta["team_id"] = team
    return SimpleNamespace(meta=meta, turn_index=1)


def test_lesson_tagged_with_team() -> None:
    """契约：有团队上下文的教训写入必须带团队标签——
    注入侧据此隔离（红：现状无标签，多团队混合）。"""
    h = RecordingHierarchy()
    _record_lesson(_ctx(h, team="t1"), "任意任务", Verdict(action="rework", reason="参数格式错误"))
    assert h.saved
    _, lesson, _ = h.saved[0]
    assert "[教训|team=t1]" in lesson, f"教训未带团队标签：{lesson}"


def test_lesson_without_team_untagged() -> None:
    """契约（对照）：无团队上下文（个人链）教训保持无标签=通用可见。"""
    h = RecordingHierarchy()
    _record_lesson(_ctx(h, team=""), "任意任务", Verdict(action="fail", reason="未知错误"))
    _, lesson, _ = h.saved[0]
    assert lesson.startswith("[教训]"), lesson
    assert "|team=" not in lesson


def test_team_session_sees_own_and_untagged(tmp_path: Any) -> None:
    """契约（核心）：团队 t1 的会话注入 = 无标签通用 + t1 专属；
    不得看到 t2 专属（红：现状全量广播）。"""
    from bandi.memory.memory_manager import SCOPE_GLOBAL

    h = _hierarchy(tmp_path)
    h.save_memory(SCOPE_GLOBAL, "[教训] 通用：写文件前确认父目录", allow_shared=True)
    h.save_memory(SCOPE_GLOBAL, "[教训|team=t1] t1专属：本团队偏好", allow_shared=True)
    h.save_memory(SCOPE_GLOBAL, "[教训|team=t2] t2专属：别团队机密", allow_shared=True)

    out = h.collect_memory_context("p1", "s1", "e1", team_id="t1")
    assert "通用" in out
    assert "t1专属" in out
    assert "t2专属" not in out, f"跨团队泄漏：{out}"


def test_employee_without_team_sees_only_untagged(tmp_path: Any) -> None:
    """契约：员工无团队归属（有 employee 无 team）只注入无标签通用教训——
    无团队上下文时安全默认为仅通用（生产上 collect_memory_context 仅在
    employee 非空时才被调用，见 context_loader）。"""
    from bandi.memory.memory_manager import SCOPE_GLOBAL

    h = _hierarchy(tmp_path)
    h.save_memory(SCOPE_GLOBAL, "[教训] 通用教训", allow_shared=True)
    h.save_memory(SCOPE_GLOBAL, "[教训|team=t1] t1专属", allow_shared=True)

    out = h.collect_memory_context("p1", "s1", "e1")
    assert "通用教训" in out
    assert "t1专属" not in out, f"无团队员工看到团队专属：{out}"
