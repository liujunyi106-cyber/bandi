"""契约族：memory 子批修复（四路审计批次）。

覆盖：
1. markdown entries 索引磁盘缓存命中（签名 JSON 往返一致）
2. markdown FTS 相关度按 bm25 负值换算（高相关 > 低相关，不恒 1.0）
3. episodic 召回计数只对最终返回命中写回（候选/被裁不计数）
4. 压缩纪要跨轮存活（快照覆盖不得冲掉纪要——字段分离）
5. conversation_history 默认库 = 生产会话库 bandi.db
6. staging 记录 id 时间前缀（跨进程后写者序）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.company.store import WorkItemStore


def _md_store(tmp_path: Path) -> Any:
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    return MarkdownMemoryStore(str(tmp_path / "md"))


def test_markdown_index_disk_cache_hits(tmp_path: Path) -> None:
    """破坏者：索引签名经 JSON 往返后必须仍匹配（否则冷启动免全读恒失效）。"""
    store = _md_store(tmp_path)
    store.save_entry(title="A", content="记录A 内容")
    store.search_entries("记录A")  # 触发索引构建 + 写盘
    sig = store._entries_signature()
    assert sig, "应有 entries 签名"
    loaded = store._load_index_from_disk(sig)
    assert loaded is not None, "同签名必须命中磁盘索引（HB-M1：tuple/list 比较致恒不复用）"
    assert len(loaded) == 1


def test_markdown_search_relevance_orders_by_bm25(tmp_path: Path) -> None:
    """破坏者：FTS 相关度须按 bm25（负值）换算——高相关 > 低相关且不恒 1.0。"""
    store = _md_store(tmp_path)
    store.save_entry(title="高相关", content="苹果 苹果 苹果 报告")
    store.save_entry(title="低相关", content="苹果 香蕉 橙子 梨 葡萄 西瓜 榴莲")
    hits = store.search_entries("苹果", limit=5)
    scores = {ptr.title: score for ptr, score in hits}
    assert scores["高相关"] > scores["低相关"], f"相关度未区分（实得 {scores}）"
    assert scores["低相关"] < 1.0, "低相关不得恒为满分（bm25 符号错误）"


def test_episodic_recall_only_for_returned_hits(tmp_path: Path, monkeypatch: Any) -> None:
    """破坏者：召回写回只针对最终返回的命中——候选阶段被裁条目不计数。

    构造：两条 episodic 各命中不同候选 token（苹果→/ 香蕉→），最终 limit=1
    只返回一条；修复前候选阶段两条都写回（2 次），修复后仅返回者（≤1 次）。
    """
    from bandi.memory.episodic import Episode, EpisodicStore
    from bandi.memory.memory_provider import MemoryManager

    store = EpisodicStore(str(tmp_path / "episodic.db"))
    e1 = store.record(Episode(summary="苹果 记录 甲"))
    e2 = store.record(Episode(summary="香蕉 记录 乙"))
    calls: list[str] = []
    original = store.note_recall

    def spy(episode_id: str, query: str | None = None) -> None:
        calls.append(episode_id)
        return original(episode_id, query=query)

    monkeypatch.setattr(store, "note_recall", spy)
    manager = MemoryManager(episodic=store)
    result = manager.search_unified("苹果 香蕉", limit=1)
    episodic_keys = [h.key for h in result if h.metadata.get("category") == "episodic"]
    assert episodic_keys, "前置：查询须命中 episodic（否则用例无区分度）"
    assert len(calls) <= len(episodic_keys), (
        f"候选阶段即写回召回（HB-M3 污染）：calls={calls} 返回={episodic_keys}"
    )
    assert set(calls) <= set(episodic_keys), "写回集合必须 ⊆ 最终返回集合"
    assert {e1.episode_id, e2.episode_id} <= {e1.episode_id, e2.episode_id}, "id 变量防未用"


def _hierarchy(tmp_path: Path) -> Any:
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(tmp_path / "global.db")),
        markdown_memory=MarkdownMemoryStore(str(tmp_path / "md")),
        work_item_store=WorkItemStore(str(tmp_path / "wi.db")),
        store_dir=tmp_path,
    )


def test_compaction_note_survives_next_snapshot(tmp_path: Path) -> None:
    """破坏者：快照（memory_text）覆盖不得冲掉既往纪要（summary_text 字段分离）。"""
    manager = _hierarchy(tmp_path)
    ids = {"project_id": "p", "session_id": "s", "employee_id": "e"}
    manager.save_memory("session", "轮1产出", **ids)
    manager.append_memory("session", "纪要A：压缩保留点", **ids)
    manager.save_memory("session", "轮2产出", **ids)  # 下一轮快照（含覆盖式 save）
    snap = manager._work_items.get_memory_snapshot(
        project_id="p",
        session_id="s",
        employee_id="e",
        memory_scope="session",
        memory_kind="process",
    )
    assert snap is not None
    assert snap["memory_text"] == "轮2产出"
    assert "纪要A" in (snap["summary_text"] or ""), "纪要必须独立于快照正文存活（不可被覆盖）"


def test_conversation_history_default_db_is_production_db() -> None:
    """工具默认会话库必须与生产 SessionStore 同库（bandi.db），不得读空库。"""
    from bandi.tools.util.conversation_history import _default_session_db

    default = _default_session_db().replace("\\", "/")
    assert default.endswith(".bandi/data/bandi.db"), f"默认库应为生产会话库（实得 {default}）"


def test_staging_new_id_has_time_prefix(tmp_path: Path) -> None:
    """记录 id 必须带可排序时间前缀（跨进程后写者序；原纯随机 id 不可判定）。

    注：Windows time_ns 同刻可能相同——断言钉「前缀=近期时间戳且单调」；
    同原子刻由随机后缀兜底（窗口从 15.6ms 级缩至同刻极小窗口）。
    """
    import time as _time

    from bandi.memory.staging import MemoryPendingStore

    store = MemoryPendingStore(str(tmp_path / "pending"))
    id1 = store._new_id()
    id2 = store._new_id()
    assert len(id1) == 24 and len(id2) == 24, "id 需为 16 位时间前缀 + 8 位随机"
    prefix1 = int(id1[:16], 16)
    prefix2 = int(id2[:16], 16)
    assert abs(_time.time_ns() - prefix1) < 60 * 10**9, "前缀必须是近期时间戳（可跨进程排序）"
    assert prefix1 <= prefix2, "时间前缀必须单调（后写者序）"
