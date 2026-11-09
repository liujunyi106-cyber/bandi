# 功能：契约——失败教训详情落项目档案 entries（③
# 详情不进每轮上下文（visible）；项目根物理隔离；检索/memory_read 按需读；
# 无项目/无 manager/写失败均安全降级）。
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


def _memory_manager(tmp_path: Any) -> tuple[Any, Any]:
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.long_term import LONG_TERM_DECAY_HALF_LIFE_DAYS, LongTermMemory
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    db = tmp_path / "data"
    db.mkdir(parents=True, exist_ok=True)
    store = MarkdownMemoryStore(db, project_root=tmp_path / "proj")
    mgr = MemoryManager(
        long_term=LongTermMemory(
            str(db / "long_term.db"), decay_half_life_days=LONG_TERM_DECAY_HALF_LIFE_DAYS
        ),
        episodic=EpisodicStore(str(db / "episodic.db")),
        working=ScratchpadStore(str(db / "working.db")),
        markdown_memory=store,
    )
    return mgr, store


def _ctx(hierarchy: Any, manager: Any, *, project: str = "p1") -> SimpleNamespace:
    meta: dict[str, Any] = {
        "hierarchy_manager": hierarchy,
        "memory_manager": manager,
        "team_id": "t1",
        "session_id": "s1",
        "observing_digest": {"failed_tools": ["bash"]},
    }
    if project:
        meta["project_id"] = project
    return SimpleNamespace(meta=meta, turn_index=3)


def _detail_body(manager: Any, query: str) -> str:
    hits = manager.search_all(query)
    assert hits.get("project_md"), f"详情未落项目 entries：{query}"
    entry_key = hits["project_md"][0].key
    entry_id = entry_key.split(":", 1)[1] if ":" in entry_key else entry_key
    return manager.expand_entry(entry_id)


def test_lesson_detail_written_to_project_entries(tmp_path: Any) -> None:
    """契约（③ 核心）：rework 的详情（含任务描述）落项目 entries——
    可检索命中、可 memory_read 展开读回全文。"""
    h = RecordingHierarchy()
    mgr, _store = _memory_manager(tmp_path)
    _record_lesson(
        _ctx(h, mgr),
        "把销售数据导出为 Excel 报表",
        Verdict(action="rework", reason="字段映射错误，需重新导出"),
    )
    body = _detail_body(mgr, "销售数据")
    assert "销售数据" in body, f"详情缺任务描述：{body}"
    assert "字段映射错误" in body
    assert h.saved, "摘要仍须写 global"


def test_detail_not_in_visible_memory(tmp_path: Any) -> None:
    """契约（③ 关键）：详情不得进 visible （每轮注入面）——
    否则「不占每轮上下文」不成立。"""
    h = RecordingHierarchy()
    mgr, store = _memory_manager(tmp_path)
    _record_lesson(
        _ctx(h, mgr),
        "把销售数据导出为 Excel 报表",
        Verdict(action="rework", reason="字段映射错误"),
    )
    visible = store.load_visible_text()
    assert "销售数据" not in visible, f"详情泄漏进 visible：{visible}"
    assert "字段映射错误" not in visible


def test_no_project_no_detail_entry(tmp_path: Any) -> None:
    """契约（对照）：无项目上下文（普通会话）不写详情；摘要照写。"""
    h = RecordingHierarchy()
    mgr, _store = _memory_manager(tmp_path)
    _record_lesson(
        _ctx(h, mgr, project=""),
        "把销售数据导出为 Excel 报表",
        Verdict(action="fail", reason="未知错误"),
    )
    assert not mgr.search_all("销售数据").get("project_md")
    assert h.saved


def test_detail_write_failure_is_safe(tmp_path: Any) -> None:
    """契约（破坏者视角）：详情写入抛异常不得影响教训主链路（摘要仍写）。"""
    h = RecordingHierarchy()

    class ExplodingManager:
        def save_entry(self, title: str, content: str) -> str:
            raise OSError("disk full")

    _record_lesson(
        _ctx(h, ExplodingManager()),
        "任意任务",
        Verdict(action="rework", reason="原因"),
    )
    assert h.saved, "详情失败后摘要必须仍写"


def test_detail_truncated_upper_bound(tmp_path: Any) -> None:
    """契约：详情有紧凑上界（防超长任务描述写爆档案）。"""
    h = RecordingHierarchy()
    mgr, _store = _memory_manager(tmp_path)
    _record_lesson(
        _ctx(h, mgr),
        "任务描述" * 500,
        Verdict(action="fail", reason="原因" * 500),
    )
    hits = mgr.search_all("任务描述")
    entry_key = hits["project_md"][0].key
    body = mgr.expand_entry(entry_key.split(":", 1)[1])
    assert len(body) <= 1500, f"详情超上界：{len(body)}"


def test_run_agent_forwards_project_root(monkeypatch: Any) -> None:
    """契约（③ 接线，DoD-11）：run_agent 必须透传 project_root——否则员工链路
    MemoryManager 落全局 fallback，详情档案不落项目根（装配缺参）。"""
    import asyncio

    from bandi.core import agent_runner

    captured: dict[str, Any] = {}

    # agent_runner 必须先于 factory 导入——反向顺序触发循环导入半初始化
    # （dataclass 装饰器拿到半成函数），故与上一 import 块以语句断开、单独导入
    from bandi.agent import factory

    captured: dict[str, Any] = {}

    class FakeResult:
        reply = "ok"
        terminal_reason = "VERIFIED"
        total_tokens = 1
        turn_index = 0

    async def fake_turn(**kwargs: Any) -> FakeResult:
        captured.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(agent_runner, "run_agent_turn", fake_turn)
    asyncio.run(factory.run_agent(user_input="hi", project_root="D:/proj-x"))
    assert captured.get("project_root") == "D:/proj-x"
