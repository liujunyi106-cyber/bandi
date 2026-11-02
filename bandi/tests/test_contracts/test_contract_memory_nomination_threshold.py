# 功能：契约——晋升提名门槛：高价值情节须被真实召回 ≥2 次（importance≥0.7）
#
# 行为不变式（钉行为，不钉实现）：
#   1. importance≥0.7 且 recall=1 的情节不提名——单次相似召回不构成复用信号
#   2. recall≥2 才提名，走暂存区等批准（无人值守路径不直写长期层）
#   3. 巩固（consolidation）与维护（maintenance）两路门槛同源
#   4. 破坏者视角：importance=1.0 也不能让 recall=1 的情节越过门槛
#   5. multi_query 通道（低重要性但被 ≥2 次不同查询召回）须带真实信号——
#      importance < 0.5 的纯普通轮即便被召回也不得走复用通道（真机实证修正）

from __future__ import annotations

from typing import Any

import pytest

from bandi.memory.consolidation import consolidate_episodes
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import LongTermMemory
from bandi.memory.maintenance import promote_notable_episodes
from bandi.memory.staging import MemoryPendingStore, reset_pending_store


@pytest.fixture()
def pending_store(tmp_path: Any):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _seed_episode(
    episodic: EpisodicStore,
    *,
    episode_id: str,
    importance: float,
    recall_count: int,
    queries: tuple[str, ...] = (),
) -> None:
    episodic.record(
        Episode(
            episode_id=episode_id,
            summary="结论：高价值经验，统一走暂存审批。",
            importance=importance,
            recall_count=recall_count,
            recall_queries=list(queries),
        )
    )


class TestConsolidationThreshold:
    def test_recall_one_not_nominated(self, tmp_path: Any, pending_store: Any) -> None:
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(episodic, episode_id="r1", importance=0.8, recall_count=1)

        report = consolidate_episodes(episodic, long_term)

        assert report.staged == [], "recall=1 的高价值情节不得提名"
        assert report.promoted == []
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()

    def test_importance_one_recall_one_still_blocked(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        """破坏者视角：满重要性也不能让单次召回越过门槛。"""
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(episodic, episode_id="r-max", importance=1.0, recall_count=1)

        report = consolidate_episodes(episodic, long_term)

        assert report.staged == [], "importance=1.0 + recall=1 仍不得提名"
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()

    def test_recall_two_nominated(self, tmp_path: Any, pending_store: Any) -> None:
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(episodic, episode_id="r2", importance=0.8, recall_count=2)

        report = consolidate_episodes(episodic, long_term)

        assert report.staged == ["r2"], "recall≥2 才提名"
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].action == "consolidate_episode"
        episodic.close()
        long_term.close()

    def test_multi_query_channel_requires_min_importance(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        """纯基准普通轮（importance 0.4）被召回也不得走复用通道——否则
        普通对话会被 n-gram 召回互相 bump 顶成提名噪音（真机实证）。"""
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(
            episodic,
            episode_id="mq-plain",
            importance=0.4,
            recall_count=4,
            queries=("q1", "q2", "q3", "q4"),
        )

        report = consolidate_episodes(episodic, long_term)

        assert report.staged == [], "0.4 基准普通轮不得走 multi_query 通道"
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()

    def test_multi_query_channel_still_works_above_floor(
        self, tmp_path: Any, pending_store: Any
    ) -> None:
        """带真实信号（importance ≥ 0.5）的复用经验仍走 multi_query 通道。"""
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(
            episodic,
            episode_id="mq",
            importance=0.55,
            recall_count=2,
            queries=("q1", "q2"),
        )

        report = consolidate_episodes(episodic, long_term)

        assert report.staged == ["mq"], "带信号的复用经验维持通道语义"
        episodic.close()
        long_term.close()

    def test_multi_importance_floor_is_exclusive(self, tmp_path: Any, pending_store: Any) -> None:
        """破坏者：0.49 贴近底线仍拦；多查询数量堆叠不改变结论。"""
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(
            episodic,
            episode_id="mq-edge",
            importance=0.49,
            recall_count=5,
            queries=("q1", "q2", "q3", "q4", "q5"),
        )

        report = consolidate_episodes(episodic, long_term)

        assert report.staged == [], "0.49 < 0.5 底线不得提名"
        episodic.close()
        long_term.close()


class TestMaintenanceThreshold:
    def test_recall_one_not_promoted(self, tmp_path: Any, pending_store: Any) -> None:
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(episodic, episode_id="m1", importance=0.5, recall_count=1)

        promoted = promote_notable_episodes(episodic, long_term)

        assert list(promoted) == [], "维护路径 recall=1 不得晋升（与巩固同源门槛）"
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()

    def test_recall_two_promoted(self, tmp_path: Any, pending_store: Any) -> None:
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(episodic, episode_id="m2", importance=0.5, recall_count=2)

        promoted = promote_notable_episodes(episodic, long_term)

        assert list(promoted) == ["m2"]
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].action == "promote_episode"
        episodic.close()
        long_term.close()

    def test_same_threshold_as_consolidation(self, tmp_path: Any, pending_store: Any) -> None:
        """同源验证：同一条 recall=1 情节在两路都不被提名。"""
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_episode(episodic, episode_id="same", importance=0.9, recall_count=1)

        assert consolidate_episodes(episodic, long_term).staged == []
        assert list(promote_notable_episodes(episodic, long_term)) == []
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()
