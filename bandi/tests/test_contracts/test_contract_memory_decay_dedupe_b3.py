"""契约：整合去重（dedupe_key 合并 + consolidated 放开）+ 长期记忆衰减/遗忘。

计划出处：`docs/记忆工程与本体升级计划-2026-10-13.md` §3.1/§3.3。

行为不变式（钉行为，不钉实现）：

去重（§3.1）
  1. 同 structured key 的两条长期记忆写入后**只存一条**——证据合并、importance 取高
  2. 同 key 但内容确有差异时按冲突处理，不得静默覆盖成一条错的内容
  3. **弱 key 不得仅凭 kind+key 合并**——`preference`/`fact` 属性未知，须退回内容指纹
  4. `consolidated:` 与 `turn:` 同权参与合并（只 `compaction:` 排除）
  5. 不同内容保持 2 条（防过度合并）

衰减/遗忘（§3.3）
  6. 持久化衰减 `importance *= 0.5^(Δdays/90)`，**Δ 从上次衰减或召回起算**——
     3 年前创建、昨天刚召回的条目不得被一次衰减打到地板
  7. 近 30 天被召回者跳过衰减
  8. 显式「记住」条目**永不删除**（仅降权）
  9. 遗忘条件全满足才删：衰减后 <0.15 且龄 >180d 且 recall=0 且非显式
 10. 批量遗忘不得清空全库（Hermes #103419 事故口径）
 11. 召回可回升 importance（用进废退闭环，否则衰减单向不可逆）
 12. importance 有 floor，归零后不可被 prune 误删

参考源：LeAgent `maintenance.py:144-172`（衰减 floor + 召回保护）、
Hermes `memory_tool_store.py:349`（拒绝清空）、`holographic/retrieval.py:52-71`
（`score *= 0.5^(age/half_life)`）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bandi.memory.long_term import LongTermMemory, MemoryItem


@pytest.fixture()
def store(tmp_path: Path) -> Any:
    instance = LongTermMemory(str(tmp_path / "lt.db"))
    yield instance
    instance.close()


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


def _item(item_id: str, content: str, **kwargs: Any) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        content=content,
        importance=kwargs.pop("importance", 0.5),
        metadata=kwargs.pop("metadata", None),
        timestamp=kwargs.pop("timestamp", None),
    )


# -- §3.1 去重 ---------------------------------------------------------------


class TestDedupeKeyMerge:
    def test_same_key_merges_into_single_item(self, store: LongTermMemory) -> None:
        """核心：同 structured key 写入两条 → 最终只存一条。"""
        from bandi.memory.merge import dedupe_key, merge_on_write

        first = _item(
            "consolidated:ep:a",
            "周报导出格式为 Markdown",
            metadata={"structured": {"kind": "preference", "key": "export_format"}},
        )
        second = _item(
            "consolidated:ep:b",
            "周报导出格式为 Markdown",
            metadata={"structured": {"kind": "preference", "key": "export_format"}},
        )
        assert dedupe_key(first) == dedupe_key(second), "同 key 必须算出同一去重键"

        store.store(first)
        merged_into = merge_on_write(store, second, dedupe_key(second))
        assert merged_into is not None, "同 key 第二次写入必须走合并，不得双插"

        items = store.list_items(limit=50)
        same_key = [
            it
            for it in items
            if (it.metadata or {}).get("structured", {}).get("key") == "export_format"
        ]
        assert len(same_key) == 1, f"同 key 留下 {len(same_key)} 条：{[i.id for i in same_key]}"

    def test_merge_keeps_higher_importance_and_unions_evidence(self, store: LongTermMemory) -> None:
        """合并取高 importance + 证据并集，不得丢事实。"""
        from bandi.memory.merge import dedupe_key, merge_on_write

        store.store(
            _item(
                "consolidated:ep:a",
                "预算口径为人民币",
                importance=0.4,
                metadata={
                    "structured": {"kind": "fact", "key": "budget_caliber"},
                    "evidence": ["预算口径统一用人民币"],
                },
            )
        )
        incoming = _item(
            "consolidated:ep:b",
            "预算口径为人民币，出表按季度",
            importance=0.9,
            metadata={
                "structured": {"kind": "fact", "key": "budget_caliber"},
                "evidence": ["出表按季度"],
            },
        )
        merge_on_write(store, incoming, dedupe_key(incoming))

        target = store.get("consolidated:ep:a")
        assert target is not None
        assert target.importance == pytest.approx(0.9), "合并须保留更高 importance"
        evidence = (target.metadata or {}).get("evidence") or []
        assert "出表按季度" in evidence, "新证据必须并入"
        assert "预算口径统一用人民币" in evidence, "旧证据不得丢"

    def test_weak_key_does_not_merge_on_key_alone(self, store: LongTermMemory) -> None:
        """破坏者：弱 key（preference/fact）属性未知，不得仅凭 key 合并。

        「我偏好简洁回答」与「我偏好详细解释」同属 `preference` 键，但属性
        完全不同——必须退回内容指纹判定，否则一条记忆会吞掉另一条。
        """
        from bandi.memory.merge import dedupe_key

        concise = _item(
            "consolidated:ep:a",
            "用户偏好简洁回答，不要长篇大论",
            metadata={"structured": {"kind": "preference", "key": "preference"}},
        )
        verbose = _item(
            "consolidated:ep:b",
            "用户偏好详细解释，需要完整推导过程",
            metadata={"structured": {"kind": "preference", "key": "preference"}},
        )
        assert dedupe_key(concise) != dedupe_key(verbose), "弱 key 必须退回内容指纹"

    def test_strong_key_merges_despite_content_wording(self, store: LongTermMemory) -> None:
        """强 key 下措辞不同仍应合并（同一属性的不同表述）。"""
        from bandi.memory.merge import dedupe_key

        a = _item(
            "consolidated:ep:a",
            "周报导出为 Markdown",
            metadata={"structured": {"kind": "preference", "key": "export_format"}},
        )
        b = _item(
            "consolidated:ep:b",
            "周报的导出格式我选 Markdown",
            metadata={"structured": {"kind": "preference", "key": "export_format"}},
        )
        assert dedupe_key(a) == dedupe_key(b)

    def test_no_structured_falls_back_to_content_fingerprint(self, store: LongTermMemory) -> None:
        """无 structured 时按内容指纹——同内容同键，不同内容不同键。"""
        from bandi.memory.merge import dedupe_key

        a = _item("turn:s1", "部署端口是 7421")
        b = _item("turn:s2", "部署端口是 7421")
        c = _item("turn:s3", "部署端口是 8421")
        assert dedupe_key(a) == dedupe_key(b)
        assert dedupe_key(a) != dedupe_key(c)


class TestConsolidatedMergeParticipation:
    def test_consolidated_participates_in_merge(self, store: LongTermMemory) -> None:
        """`consolidated:` 必须与 `turn:` 同权参与合并（只 `compaction:` 排除）。"""
        from bandi.memory.merge import suggest_merges

        for idx in ("a", "b"):
            store.store(
                _item(
                    f"consolidated:ep:{idx}",
                    "部署端口是 7421，上线时间定在周三",
                    metadata={"memory_type": "business_fact"},
                )
            )
        store.store(
            _item(
                "compaction:c",
                "部署端口是 7421，上线时间定在周三",
                metadata={"memory_type": "business_fact"},
            )
        )

        suggestions = suggest_merges(store, similarity_threshold=0.6)
        sources = {sid for s in suggestions for sid in s.source_ids}
        assert any(s.startswith("consolidated:") for s in sources), (
            f"consolidated: 未参与合并，实际来源 {sources}"
        )
        assert not any(s.startswith("compaction:") for s in sources), "compaction: 必须仍被排除"


# -- §3.3 衰减 / 遗忘 --------------------------------------------------------


class TestPersistentDecay:
    def test_importance_decays_by_half_life(self, store: LongTermMemory) -> None:
        """90 天整 = importance × 0.5（数学精确断言）。"""
        from bandi.memory.decay import decay_items

        store.store(_item("old", "很久以前的结论", importance=1.0, timestamp=time_ts(90)))
        report = decay_items(store, now=datetime.now(UTC))
        assert report.decayed == 1
        assert store.get("old").importance == pytest.approx(0.5, abs=0.02)

    def test_recently_recalled_is_skipped(self, store: LongTermMemory) -> None:
        """近 30 天被召回者跳过衰减（用进废退的另一面）。"""
        from bandi.memory.decay import decay_items

        store.store(
            _item(
                "hot",
                "高频使用的结论",
                importance=1.0,
                timestamp=time_ts(200),
                metadata={"recall_count": 3, "last_recalled_at": _iso(5)},
            )
        )
        report = decay_items(store, now=datetime.now(UTC))
        assert report.skipped_recent_recall == 1, "近期召回条目应被计入豁免"
        assert store.get("hot").importance == pytest.approx(1.0), "近期召回条目不得衰减"

    def test_decay_measured_from_last_recall_not_creation(self, store: LongTermMemory) -> None:
        """破坏者：Δ 从上次召回起算，不是从创建起算。

        3 年前创建、昨天刚召回的条目若按创建时间算，会一次性经历
        12 个半衰期被打到地板——与「最近才用过」的事实矛盾。
        """
        from bandi.memory.decay import decay_items

        store.store(
            _item(
                "reused",
                "老条目但刚被召回",
                importance=1.0,
                timestamp=time_ts(1000),
                metadata={"recall_count": 1, "last_recalled_at": _iso(1)},
            )
        )
        decay_items(store, now=datetime.now(UTC))
        assert store.get("reused").importance > 0.9, "刚召回的老条目几乎不该衰减"

    def test_importance_has_floor(self, store: LongTermMemory) -> None:
        """衰减不得把 importance 打到 0——归零后会被 prune 按 importance ASC 误删。"""
        from bandi.memory.decay import DECAY_IMPORTANCE_FLOOR, decay_items

        store.store(_item("ancient", "远古条目", importance=0.5, timestamp=time_ts(5000)))
        decay_items(store, now=datetime.now(UTC))
        assert store.get("ancient").importance >= DECAY_IMPORTANCE_FLOOR

    def test_recall_restores_importance(self, store: LongTermMemory) -> None:
        """召回可回升 importance——否则衰减单向不可逆，系统会逐渐空。"""
        from bandi.memory.decay import RECALL_IMPORTANCE_BOOST

        store.store(_item("m", "会被用到的事实", importance=0.3))
        store.note_recalled("m")
        boosted = store.get("m").importance
        assert boosted > 0.3, "召回后 importance 应回升"
        assert boosted <= 0.3 + RECALL_IMPORTANCE_BOOST + 1e-6, "回升不得超过单次上限"


def time_ts(days_ago: float) -> float:
    return (datetime.now(UTC) - timedelta(days=days_ago)).timestamp()


class TestForgetRules:
    def test_low_value_stale_never_recalled_is_deleted(self, store: LongTermMemory) -> None:
        """衰减后 <0.15 + 龄 >180d + recall=0 + 非显式 → 删。

        另置一条受保护条目，使库内不只有可删项——否则会命中
        「拒绝清空」安全阀（那是另一条契约的行为）。
        """
        from bandi.memory.decay import forget_stale_items

        store.store(
            _item(
                "forgettable",
                "早就没人用的碎片",
                importance=0.05,
                timestamp=time_ts(400),
                metadata={"recall_count": 0},
            )
        )
        store.store(_item("protected", "还在用的条目", importance=0.9, timestamp=time_ts(1)))
        deleted = forget_stale_items(store, now=datetime.now(UTC))
        assert "forgettable" in deleted
        assert store.get("forgettable") is None
        assert store.get("protected") is not None

    @pytest.mark.parametrize(
        ("item_id", "kwargs", "why"),
        [
            ("fresh", {"importance": 0.05, "timestamp": time_ts(10)}, "龄不足 180d"),
            (
                "used",
                {"importance": 0.05, "timestamp": time_ts(400), "metadata": {"recall_count": 2}},
                "被召回过",
            ),
            (
                "valuable",
                {"importance": 0.9, "timestamp": time_ts(400), "metadata": {"recall_count": 0}},
                "importance 未低于阈值",
            ),
        ],
        ids=["too-fresh", "was-recalled", "still-valuable"],
    )
    def test_forget_keeps_protected_items(
        self, store: LongTermMemory, item_id: str, kwargs: Any, why: str
    ) -> None:
        """破坏者：任一保护条件不满足就不得删除。"""
        from bandi.memory.decay import forget_stale_items

        store.store(_item(item_id, f"受保护的条目 {item_id}", **kwargs))
        forget_stale_items(store, now=datetime.now(UTC))
        assert store.get(item_id) is not None, f"{why} 却仍被删除"

    def test_explicit_memory_never_deleted(self, store: LongTermMemory) -> None:
        """显式「记住」条目永不删除——用户明说要记的东西不能被衰减清掉。"""
        from bandi.memory.decay import forget_stale_items

        store.store(
            _item(
                "explicit",
                "用户显式要求记住的地址",
                importance=0.02,
                timestamp=time_ts(900),
                metadata={"recall_count": 0, "is_explicit": True},
            )
        )
        deleted = forget_stale_items(store, now=datetime.now(UTC))
        assert "explicit" not in deleted, "显式条目被删了"
        assert store.get("explicit") is not None

    def test_explicit_memory_still_decays(self, store: LongTermMemory) -> None:
        """显式条目只降权、不豁免衰减（否则永不衰减的显式条目会永久霸占首位）。"""
        from bandi.memory.decay import decay_items

        store.store(
            _item(
                "explicit2",
                "显式但陈旧的记忆",
                importance=1.0,
                timestamp=time_ts(200),
                metadata={"is_explicit": True},
            )
        )
        decay_items(store, now=datetime.now(UTC))
        assert store.get("explicit2").importance < 1.0, "显式条目也应衰减"

    def test_bulk_forget_refuses_to_empty_store(self, store: LongTermMemory) -> None:
        """破坏者：批量遗忘不得清空全库（Hermes #103419 事故口径）。

        若全部条目都满足删除条件，一次维护就会把长期记忆清空——
        此时必须整批放弃。
        """
        from bandi.memory.decay import forget_stale_items

        for idx in range(3):
            store.store(
                _item(
                    f"stale{idx}",
                    f"陈旧碎片 {idx}",
                    importance=0.02,
                    timestamp=time_ts(500),
                    metadata={"recall_count": 0},
                )
            )
        deleted = forget_stale_items(store, now=datetime.now(UTC))
        assert deleted == [], "全部条目可删时必须整批放弃，防清空"
        assert store.count() == 3, "条目不应被清空"

    def test_env_switch_disables_forget(
        self, store: LongTermMemory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`BANDI_MEMORY_FORGET=0` 可关遗忘（调试/取证期间保护数据）。"""
        from bandi.memory.decay import forget_stale_items

        monkeypatch.setenv("BANDI_MEMORY_FORGET", "0")
        store.store(
            _item(
                "keepme",
                "本该被删的条目",
                importance=0.02,
                timestamp=time_ts(500),
                metadata={"recall_count": 0},
            )
        )
        assert forget_stale_items(store, now=datetime.now(UTC)) == []
        assert store.get("keepme") is not None


class TestExplicitMarkerWiring:
    def test_explicit_remember_marks_metadata(self) -> None:
        """生产链路：显式「记住」必须落 `is_explicit`，否则遗忘豁免永不生效。

        断言生产函数在显式输入下产出带标记的 metadata——不 grep 源码，
        走真实 `_persist_memory` 主链路。
        """
        import asyncio
        from types import SimpleNamespace

        from bandi.core.agent_runner import _build_memory_manager, _persist_memory

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("HOME", str(tmp_home := Path(__import__("tempfile").mkdtemp())))
            mp.setenv("USERPROFILE", str(tmp_home))
            manager = _build_memory_manager(tmp_home / "data")
            try:
                ctx = SimpleNamespace(
                    tool_results=None,
                    final_message="收到，已记录你的部署端口 7421。",
                    current_content=None,
                    raw_input="记住，我们的部署端口是 7421",
                    turn_index=1,
                    terminal_reason=SimpleNamespace(name="VERIFIED"),
                    messages=[],
                )
                asyncio.run(_persist_memory(ctx, manager, session_id="b3-explicit"))
                items = manager.long_term.list_items(limit=20)
                explicit_items = [i for i in items if (i.metadata or {}).get("is_explicit")]
                assert explicit_items, f"显式「记住」未落标记：{[i.id for i in items]}"
            finally:
                manager.close()

    def test_ordinary_turn_not_marked_explicit(self) -> None:
        """破坏者：普通陈述不得被标成显式（否则永不删除，保护形同虚设）。"""
        from bandi.memory.ingest_review import has_explicit_remember

        assert has_explicit_remember("我们的部署端口是 7421") is False
        assert has_explicit_remember("记住，我偏好简洁回答") is True

    def test_promotion_path_carries_explicit_flag(self, tmp_path: Path) -> None:
        """晋升路径必须带显式标记——直写有豁免而晋升没有的话，同一句话
        会因落库路径不同而命运不同（晋升后被遗忘删掉）。"""
        from bandi.memory.decay import explicit_flags
        from bandi.memory.episodic import Episode

        explicit = Episode(
            episode_id="e1",
            summary="用户: 记住，我永远用 pnpm → 回答: 好的",
            user_input="记住，我永远用 pnpm",
        )
        ordinary = Episode(
            episode_id="e2",
            summary="用户: pnpm 是什么 → 回答: 包管理器",
            user_input="pnpm 是什么",
        )
        assert explicit_flags(explicit) == {"is_explicit": True}
        assert explicit_flags(ordinary) == {}


class TestMaintenanceWiring:
    def test_run_memory_maintenance_includes_long_term_decay(self, tmp_path: Path) -> None:
        """维护周期必须跑长期层衰减/遗忘——否则功能永不自动执行。

        走真实 `run_memory_maintenance`（生产入口），断言报告含衰减字段，
        且陈旧低分条目确实被删。
        """
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory, MemoryItem
        from bandi.memory.maintenance import run_memory_maintenance

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        try:
            long_term.store(
                MemoryItem(
                    item_id="stale",
                    content="陈旧低分条目",
                    importance=0.02,
                    timestamp=(datetime.now(UTC) - timedelta(days=400)).timestamp(),
                    metadata={"recall_count": 0},
                )
            )
            long_term.store(MemoryItem("keeper", "要保留的条目", importance=0.9, timestamp=None))
            report = run_memory_maintenance(episodic, long_term)
            assert "long_term_decayed" in report, "维护报告须含长期层衰减结果"
            assert "long_term_forgotten" in report, "维护报告须含长期层遗忘结果"
            assert long_term.get("stale") is None, "陈旧低分条目应被维护周期删除"
            assert long_term.get("keeper") is not None, "高价值条目不得被删"
        finally:
            episodic.close()
            long_term.close()

    def test_decay_can_be_skipped_by_caller(self, tmp_path: Path) -> None:
        """关闭开关（测试/取证场景）后维护仍跑其余项，不做长期层衰减。"""
        from bandi.memory.episodic import EpisodicStore
        from bandi.memory.long_term import LongTermMemory, MemoryItem
        from bandi.memory.maintenance import run_memory_maintenance

        episodic = EpisodicStore(str(tmp_path / "ep2.db"))
        long_term = LongTermMemory(str(tmp_path / "lt2.db"))
        try:
            long_term.store(
                MemoryItem(
                    item_id="keep",
                    content="不衰减的条目",
                    importance=0.02,
                    timestamp=(datetime.now(UTC) - timedelta(days=400)).timestamp(),
                    metadata={"recall_count": 0},
                )
            )
            report = run_memory_maintenance(episodic, long_term, decay_long_term=False)
            assert "long_term_decayed" not in report
            assert long_term.get("keep") is not None
        finally:
            episodic.close()
            long_term.close()


def test_note_recalled_never_lowers_high_importance(tmp_path: Path) -> None:
    """破坏者（B8 暴露）：召回回写只准升不准降——高值条目不得被回升封顶削级。

    修复前 true bug：boosted = min(0.6, cur+0.02) 会把 0.8 的记忆召回后降到 0.6
    （越常被读权重反而掉）；修复后 = max(cur, min(0.6, cur+0.02))。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem

    store = LongTermMemory(str(tmp_path / "lt_recall.db"))
    try:
        store.store(MemoryItem(item_id="hi", content="高价值条目", importance=0.8))
        store.note_recalled("hi")
        assert store.get("hi").importance == 0.8, "高值条目召回后不得被降级"
        store.store(MemoryItem(item_id="lo", content="低保条目", importance=0.3))
        store.note_recalled("lo")
        assert abs(store.get("lo").importance - 0.32) < 1e-9, "低值条目仍按单次加成回升"
    finally:
        store.close()
