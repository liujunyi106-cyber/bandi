"""契约：prefetch/memory_source 去重统一 memory_id + SEEN 持久化。

生产调用路径：
1. context.sources.memory_source.MemoryContextSource.collect——注入记账（SEEN_BY_SESSION）
2. context.sources.memory_source.load_seen_state——重启恢复（跨进程不丢）
3. agent.states._filter_prefetch_payload——prefetch 按注入 keys 剔除
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bandi.context.sources import memory_source as ms
from bandi.core.message import Message, MessageRole
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.markdown_memory import EntryPointer, MarkdownMemoryStore
from bandi.memory.memory_manager import (
    _SCOPE_VALUE,
    MemoryBlock,
    MemoryHierarchyManager,
)
from bandi.memory.memory_provider import MemoryManager


class _FakeManager:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
        return self._entries


def _entry(category: str, key: str, value: str = "记忆内容") -> Any:
    class _E:
        def __init__(self, k: str, v: str, meta: dict[str, str]) -> None:
            self.key = k
            self.value = v
            self.metadata = meta

    return _E(key, value, {"category": category})


def test_seen_state_persists_across_reload(tmp_path: Path, monkeypatch: Any) -> None:
    ms.SEEN_BY_SESSION.clear()
    monkeypatch.setattr(ms, "_seen_store_path", lambda: tmp_path / "seen_memory.json")
    source = ms.MemoryContextSource(memory_manager=_FakeManager([_entry("long_term", "k1")]))

    async def run() -> None:
        await source.collect(Message(role=MessageRole.USER, content="查询"), "s-persist")

    import asyncio

    asyncio.run(run())
    assert (tmp_path / "seen_memory.json").exists(), "注入后必须持久化 seen 状态"
    data = json.loads((tmp_path / "seen_memory.json").read_text(encoding="utf-8"))
    assert "s-persist" in data and "long_term:k1" in data["s-persist"], (
        f"seen 必须含命名空间化 memory_id（实际 {data}）"
    )

    # 模拟重启：清空容器后从文件恢复
    ms.SEEN_BY_SESSION.clear()
    ms.load_seen_state()
    assert ms.SEEN_BY_SESSION.get("s-persist", {}).get("long_term:k1", 0) == 1, "重启后计数恢复"


def test_cross_category_same_key_does_not_collide(tmp_path: Path, monkeypatch: Any) -> None:
    ms.SEEN_BY_SESSION.clear()
    monkeypatch.setattr(ms, "_seen_store_path", lambda: tmp_path / "seen_memory.json")
    source = ms.MemoryContextSource(
        memory_manager=_FakeManager(
            [
                _entry("long_term", "same-id"),
                _entry("markdown", "same-id"),
            ]
        )
    )

    async def run() -> None:
        await source.collect(Message(role=MessageRole.USER, content="查询"), "s-multi")

    import asyncio

    asyncio.run(run())
    seen = ms.SEEN_BY_SESSION["s-multi"]
    assert seen.get("long_term:same-id", 0) == 1
    assert seen.get("markdown:same-id", 0) == 1, "跨分类同 key 不得互相覆盖（命名空间隔离）"


# ==================== f2_fusion_default ====================


class _LT:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def search_fts_ranked(self, query, limit=5, project_id=None, employee_id=None):
        return self._rows

    def close(self) -> None:
        pass


class _Row:
    def __init__(
        self, rid: str, content: str, importance: float, ts: datetime, score: float = 1.0
    ) -> None:
        self.id = rid
        self.content = content
        self.importance = importance
        self.timestamp = ts.isoformat()
        self.score = score


def _mk_manager(rows: list) -> MemoryManager:
    lt = _LT(rows)
    mgr = MemoryManager(long_term=lt)
    return mgr


def test_default_scoring_is_fusion(tmp_path) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    fresh = datetime.now(UTC) - timedelta(days=1)
    stale_hi = _Row("lt-stale", "蓝鲸 迁徙 路线 老数据", 0.9, old)
    fresh_low = _Row("lt-fresh", "蓝鲸 迁徙 路线 新发现", 0.4, fresh)
    mgr = _mk_manager([stale_hi, fresh_low])

    out_default = mgr.search_unified("蓝鲸")
    out_fusion = mgr.search_unified("蓝鲸", scoring="fusion")
    assert [h.key for h in out_default] == [h.key for h in out_fusion], (
        f"默认必须等于显式 fusion（实际 {[h.key for h in out_default]} vs {[h.key for h in out_fusion]}）"
    )
    # 显式 classified 仍可用（旧模式保留）
    out_cls = mgr.search_unified("蓝鲸", scoring="classified")
    assert out_cls, "classified 模式必须仍可用"


def test_intent_hint_weights_factual_category(tmp_path) -> None:
    from bandi.memory.working import ScratchHit

    class _WK:
        def search(self, query, limit=5, session_id=None):
            hit = ScratchHit(kind="kv", key="1", value="蓝鲸 草稿 数据")
            return [(hit, 0.3)]

        def close(self) -> None:
            pass

    old = datetime.now(UTC) - timedelta(days=60)
    lt_row = _Row("lt-fact", "蓝鲸 迁徙 路线 数据", 0.5, old)
    mgr = MemoryManager(long_term=_LT([lt_row]), working=_WK())

    # factual intent：事实类（long_term）加权后优先于 working 草稿
    out = mgr.search_unified("蓝鲸", intent_hint="factual", scoring="fusion")
    assert out and out[0].key == "lt-fact", (
        f"factual intent 必须加权事实类（实际 {[h.key for h in out]}）"
    )

    # procedural intent：working 草稿加权后优先
    out2 = mgr.search_unified("蓝鲸", intent_hint="procedural", scoring="fusion")
    assert out2 and out2[0].key == "kv:1", (
        f"procedural intent 必须加权 working（实际 {[h.key for h in out2]}）"
    )


def test_fair_quota_distributes_by_category_share(tmp_path) -> None:
    from bandi.memory.working import ScratchHit

    class _WK:
        def search(self, query, limit=5, session_id=None):
            return [
                (ScratchHit(kind="kv", key=f"{i}", value=f"蓝鲸 数据 变体{i}"), 0.5)
                for i in range(3)
            ]

        def close(self) -> None:
            pass

    lt_rows = [
        _Row(f"lt-{i}", "蓝鲸 迁徙", 0.5, datetime.now(UTC) - timedelta(days=10)) for i in range(1)
    ]
    mgr = MemoryManager(long_term=_LT(lt_rows), working=_WK())

    out = mgr.search_unified("蓝鲸", limit=4, scoring="fusion")
    cats = {h.metadata.get("category") for h in out}
    assert len(cats) >= 2, f"公平配额必须跨类别分布（实际 {cats}）"
    counts = {}
    for h in out:
        counts[h.metadata.get("category")] = counts.get(h.metadata.get("category"), 0) + 1
    assert counts.get("working", 0) > counts.get("long_term", 0), (
        f"候选多的分类拿更多配额（实际 {counts}）"
    )


# ==================== f3_hierarchy_dedup ====================


class _HierarchyStub:
    def __init__(self, text: str) -> None:
        self._text = text

    def collect_memory_context(self, **kwargs: Any) -> str:
        return self._text


class _SessionStoreStub:
    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return None


def test_hierarchy_injection_marks_entry_ids_in_seen(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    from bandi.perception.context_loader import ContextLoader

    ms.SEEN_BY_SESSION.clear()
    monkeypatch.setattr(ms, "_seen_store_path", lambda: tmp_path / "seen.json")
    hierarchy_text = (
        "# 项目惯例\n"
        "- [蓝鲸项目文档](entries/abc123.md) — 关键决策\n"
        "- [部署手册](entries/def456.md) — 运维要点\n"
        "# 会话快照\n"
        "process 摘要内容"
    )
    loader = ContextLoader(
        hierarchy=_HierarchyStub(hierarchy_text),
        project_id="p1",
        employee_id="e1",
        session_store=_SessionStoreStub(),
    )
    asyncio.run(loader.load(Message(role=MessageRole.USER, content="查询"), session_id="s-hier"))

    from bandi.context.sources.memory_source import HIERARCHY_INJECTED_BY_SESSION

    marked = HIERARCHY_INJECTED_BY_SESSION.get("s-hier", set())
    assert "project_md:entry:abc123" in marked, "hierarchy 注入的 entry 必须标记已注入（剔除容器）"
    assert "project_md:entry:def456" in marked


def test_hierarchy_marked_entries_downgraded_in_memory_source(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from bandi.context.item import ContextItem
    from bandi.context.sources.memory_source import (
        HIERARCHY_INJECTED_BY_SESSION,
        SEEN_BY_SESSION,
    )
    from bandi.perception.context_loader import _mark_hierarchy_injected

    class _Entry:
        def __init__(self, key: str, category: str, value: str = "内容") -> None:
            self.key = key
            self.value = value
            self.metadata = {"category": category}

    class _Mgr:
        def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
            return [
                _Entry("entry:abc123", "project_md", "蓝鲸项目文档内容"),
                _Entry("lt-1", "long_term", "蓝鲸 关键事实"),
            ]

    SEEN_BY_SESSION.clear()
    HIERARCHY_INJECTED_BY_SESSION.clear()
    monkeypatch.setattr(ms, "_seen_store_path", lambda: tmp_path / "seen.json")
    _mark_hierarchy_injected("s-hier2", "- [蓝鲸项目文档](entries/abc123.md) — 关键决策")

    source = ms.MemoryContextSource(memory_manager=_Mgr())

    async def run() -> None:
        return await source.collect(Message(role=MessageRole.USER, content="蓝鲸"), "s-hier2")

    import asyncio

    items: list[ContextItem] = asyncio.run(run())
    keys = [i.metadata.get("key") for i in items]
    assert keys == ["lt-1"], f"P2-8：hierarchy 已注入 entry 必须剔除（仅保留未注入项，实际 {keys}）"


# ==================== f4_markdown_fts ====================


def _mk_store(tmp_path: Path) -> tuple[MarkdownMemoryStore, dict[str, EntryPointer]]:
    store = MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "root")
    store.ensure_memory_file()
    return store, {}


def test_fts_retrieves_cjk_entries(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "root")
    store.ensure_memory_file()
    p1 = store.save_entry("蓝鲸迁徙路线文档", "蓝鲸 每年 迁徙 太平洋 路线")
    p2 = store.save_entry("企鹅栖息地", "企鹅 南极 栖息地 繁殖")

    hits = store.search_entries("蓝鲸迁徙", limit=5)
    ids = [ptr.entry_id for ptr, _rel in hits]
    assert p1.entry_id in ids and p2.entry_id not in ids, (
        f"FTS 必须召回中文 2-gram 命中（实际 {ids}）"
    )


def test_fts_fallback_to_ngram_on_empty(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "root")
    store.ensure_memory_file()
    p1 = store.save_entry("特殊符号条目", "!!! 特殊 标点 内容 ###")
    hits = store.search_entries("特殊 标点", limit=5)
    ids = [ptr.entry_id for ptr, _rel in hits]
    assert p1.entry_id in ids, f"FTS 空召回必须回退 n-gram 保召回（实际 {ids}）"


def test_fts_english_query(tmp_path: Path) -> None:
    store = MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "root")
    store.ensure_memory_file()
    p1 = store.save_entry("API 文档", "python sdk quickstart guide")
    p2 = store.save_entry("部署手册", "docker compose deployment")

    hits = store.search_entries("sdk quickstart", limit=5)
    ids = [ptr.entry_id for ptr, _rel in hits]
    assert p1.entry_id in ids and p2.entry_id not in ids, f"FTS 英文短语必须命中（实际 {ids}）"


# ==================== f5_prune_kind ====================


def _store(
    mem: LongTermMemory,
    item_id: str,
    importance: float,
    project_id: str | None = None,
    employee_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    mem.store(
        MemoryItem(
            item_id=item_id, content=item_id, importance=importance, metadata=metadata or {}
        ),
        project_id=project_id,
        employee_id=employee_id,
    )


def test_kind_dimension_partitions_quota(tmp_path) -> None:
    mem = LongTermMemory(str(tmp_path / "lt.db"))
    try:
        # 同 project+employee 下 3 条 turn 记忆 + 3 条 compaction 记忆
        for i in range(3):
            _store(mem, f"turn:p1:{i}", 0.3, project_id="p1", employee_id="e1")
        for i in range(3):
            _store(mem, f"compaction:p1:{i}", 0.3, project_id="p1", employee_id="e1")
        mem.prune_partitioned(max_items=100, per_partition_max=2)
        turns = [mem.get(f"turn:p1:{i}") for i in range(3)]
        comps = [mem.get(f"compaction:p1:{i}") for i in range(3)]
        assert sum(1 for t in turns if t is not None) <= 2, "turn kind 配额独立"
        assert sum(1 for c in comps if c is not None) <= 2, "compaction kind 配额独立"
        assert (
            sum(1 for t in turns if t is not None) == 2
            and sum(1 for c in comps if c is not None) == 2
        ), "kind 分区必须各自保留配额上限"
    finally:
        mem.close()


def test_last_recalled_prefers_recently_used(tmp_path) -> None:
    mem = LongTermMemory(str(tmp_path / "lt.db"))
    try:
        _store(mem, "old-recalled", 0.2, metadata={"last_recalled_at": "T00:00:00+00:00"})
        _store(mem, "old-never", 0.2)
        _store(mem, "high-imp", 0.9)
        mem.prune_partitioned(max_items=100, per_partition_max=2)
        assert mem.get("high-imp") is not None
        assert mem.get("old-recalled") is not None, "同 importance 下被召回过的优先保留"
        assert mem.get("old-never") is None, "未被召回的同价值记忆先裁"
    finally:
        mem.close()


# ==================== f6_memory_block ====================


def test_memory_block_has_full_fields() -> None:
    blk = MemoryBlock(
        text="决策：基线",
        priority=0.9,
        source="employee",
        importance=0.8,
        recency=0.6,
        confidence=1.0,
        token_cost=42,
        dependencies=("决策：前置",),
    )
    assert blk.text == "决策：基线"
    assert blk.priority == 0.9 and blk.importance == 0.8
    assert blk.token_cost == 42
    assert blk.dependencies == ("决策：前置",)


def _mk_hierarchy(tmp_path: Path) -> MemoryHierarchyManager:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.session.store import SessionStore

    h = MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(tmp_path / "global.db")),
        markdown_memory=MarkdownMemoryStore(tmp_path / "md", project_root=tmp_path / "root"),
        work_item_store=WorkItemStore(str(tmp_path / "work.db")),
        session_store=SessionStore(str(tmp_path / "sess.db")),
        store_dir=tmp_path,
    )
    return h


def test_block_packing_prefers_higher_priority_blocks(tmp_path: Path) -> None:
    """块级 packing：预算不足时高优先级（员工层）块优先装入，低优先级（全局层）
    块被裁——不再整 scope 丢弃。"""
    h = _mk_hierarchy(tmp_path)
    h.save_memory(
        "global",
        "全局惯例块A\n长正文内容超出预算的全局说明文字",
        project_id="p1",
    )
    h.save_memory(
        "employee",
        "员工专属块B\n员工级关键内容叙述正文",
        project_id="p1",
        employee_id="e1",
    )
    ctx = h.collect_memory_context(
        project_id="p1", session_id="s1", employee_id="e1", max_tokens=12
    )
    assert "员工专属块B" in ctx, "高优先级（员工层）块必须优先装入"
    assert "全局惯例块A" not in ctx, "低优先级块超预算被裁（不是整 scope 丢弃）"


def test_block_dependencies_gate_installation(tmp_path: Path) -> None:
    """依赖约束：依赖未满足的块不装入（先装依赖块）。"""
    blocks = [
        MemoryBlock(
            text="B块", priority=0.9, source="employee", token_cost=5, dependencies=("A块",)
        ),
        MemoryBlock(text="A块", priority=0.5, source="project", token_cost=5),
    ]
    packed = MemoryHierarchyManager._pack_blocks(blocks, max_tokens=20)
    texts = [b.text for b in packed]
    assert "A块" in texts and "B块" in texts, "依赖块 A 必须先装入，B 才能装"


def test_mandatory_prefix_preserved_in_block_packing(tmp_path: Path) -> None:
    """J-1d 语义保持：整块超预算时必留首行仍保留。"""
    h = _mk_hierarchy(tmp_path)
    h.save_memory(
        "employee",
        "决策：基线\n长正文说明此处压缩点细节每分钟节点记录超出预算的冗长叙述",
        project_id="proj1",
        employee_id="emp1",
    )
    ctx = h.collect_memory_context(
        project_id="proj1", session_id="s1", employee_id="emp1", max_tokens=10
    )
    assert "决策" in ctx, "必留首行标识必须在超预算时保留"
    assert "长正文说明" not in ctx, "可省正文应在预算内被裁掉"


def test_scope_priority_value_mapping() -> None:
    assert _SCOPE_VALUE["employee"] > _SCOPE_VALUE["session"]
    assert _SCOPE_VALUE["session"] > _SCOPE_VALUE["project"]
    assert _SCOPE_VALUE["project"] > _SCOPE_VALUE["global"]
