# 功能：契约——整合提名的「拒绝必须粘滞」
#
# 行为不变式（钉行为，不钉实现）：
#   1. 用户驳回 consolidate_episode 后，巩固周期重扫不得再次提名（「删了又回来」循环）
#   2. 已批准晋升的情节不得被重复提名（否则用户反复批准同一条）
#   3. 仍在暂存区等批准的情节不得重复提名（否则每轮改写暂存记录）
#   4. 拒绝标记幂等；reject 端点丢弃记录时同步打标（生产调用点接线）
#   5. 破坏者视角：后续召回计数继续增长也不得复活已拒绝的提名

from __future__ import annotations

import importlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import bandi.memory.episodic as episodic_module
from bandi.memory.consolidation import (
    PROMOTION_REJECTED_TAG,
    consolidate_episodes,
    mark_promotion_rejected,
)
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.staging import MemoryPendingStore, reset_pending_store

app_module = importlib.import_module("bandi.api.app")


@pytest.fixture()
def pending_store(tmp_path):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _seeded(tmp_path, *, episode_id: str = "rej-1"):
    """功能：真实情节库 + 长期库（高价值情节满足晋升门槛）。"""
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    long_term = LongTermMemory(str(tmp_path / "lt.db"))
    episodic.record(
        Episode(
            episode_id=episode_id,
            summary="结论：高价值经验，统一走暂存审批。",
            importance=0.8,
            recall_count=2,
        )
    )
    return episodic, long_term


def test_rejected_episode_not_renominated(tmp_path, pending_store) -> None:
    """核心不变式：驳回后重扫不得再提名。"""
    episodic, long_term = _seeded(tmp_path, episode_id="rej-1")
    report = consolidate_episodes(episodic, long_term)
    assert report.staged == ["rej-1"]
    record = pending_store.list_pending()[0]

    assert mark_promotion_rejected("rej-1", episodic) is True
    pending_store.discard_pending(record.id8)

    report2 = consolidate_episodes(episodic, long_term)
    assert report2.staged == [], "已拒绝的情节不得被再次提名（拒绝必须粘滞）"
    assert pending_store.list_pending() == []


def test_recall_growth_cannot_revive_rejected(tmp_path, pending_store) -> None:
    """破坏者视角：相似问句继续召回（计数虚高）也不得复活已拒绝提名。"""
    episodic, long_term = _seeded(tmp_path, episode_id="rej-2")
    assert mark_promotion_rejected("rej-2", episodic) is True

    episode = episodic.get("rej-2")
    episodic.record(
        replace(episode, importance=0.99, recall_count=99, recall_queries=["a", "b", "c"])
    )

    report = consolidate_episodes(episodic, long_term)
    assert report.staged == []
    assert pending_store.list_pending() == []


def test_promoted_episode_not_renominated(tmp_path, pending_store) -> None:
    """已批准晋升的情节不得被巩固周期重复提名。"""
    episodic, long_term = _seeded(tmp_path, episode_id="appr-1")
    long_term.store(
        MemoryItem(item_id="consolidated:appr-1", content="结论：高价值经验。", importance=0.8)
    )

    report = consolidate_episodes(episodic, long_term)
    assert report.staged == []
    assert pending_store.list_pending() == []


def test_pending_episode_not_renominated(tmp_path, pending_store) -> None:
    """已在暂存区等批准的情节：重扫不得改写/重复提名。"""
    episodic, long_term = _seeded(tmp_path, episode_id="pend-1")
    consolidate_episodes(episodic, long_term)
    first = pending_store.list_pending()[0]

    report2 = consolidate_episodes(episodic, long_term)
    assert report2.staged == []
    records = pending_store.list_pending()
    assert len(records) == 1
    assert records[0].id8 == first.id8, "暂存记录不得被整轮重写"


def test_mark_idempotent_and_missing_episode(tmp_path) -> None:
    """标记幂等；情节不存在时返回 False 且不抛。"""
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    assert mark_promotion_rejected("nope", episodic) is False

    episodic.record(
        Episode(episode_id="m-1", summary="结论：普通经验。", importance=0.5, recall_count=0)
    )
    assert mark_promotion_rejected("m-1", episodic) is True
    assert mark_promotion_rejected("m-1", episodic) is True
    assert episodic.get("m-1").tags.count(PROMOTION_REJECTED_TAG) == 1


def test_reject_endpoint_marks_episode(tmp_path, pending_store, monkeypatch) -> None:
    """接线契约：reject 端点丢弃暂存记录时同步打「晋升已拒绝」标记。"""
    episodic = EpisodicStore(str(tmp_path / "ep.db"))
    episodic.record(
        Episode(episode_id="epx", summary="结论：高价值经验。", importance=0.8, recall_count=1)
    )
    record = pending_store.stage(
        {
            "action": "consolidate_episode",
            "summary": "episodic_consolidation: 结论：高价值经验。",
            "origin": "background",
            "payload": {
                "target": "long_term",
                "item": {
                    "id": "consolidated:epx",
                    "content": "结论：高价值经验。",
                    "metadata": {"episode_id": "epx"},
                },
            },
        }
    )
    monkeypatch.setattr(app_module, "_get_memory_pending_store", lambda: pending_store)
    monkeypatch.setattr(episodic_module, "get_shared_episodic", lambda: episodic)
    monkeypatch.setattr(app_module, "_API_KEY", "test")

    with TestClient(app_module.app) as client:
        client.headers["Authorization"] = "Bearer test"
        response = client.post(f"/api/memory/pending/{record.id8}/reject")

    assert response.status_code == 200
    assert response.json()["rejected"] is True
    assert PROMOTION_REJECTED_TAG in episodic.get("epx").tags


def test_turn_direct_write_blocks_nomination(tmp_path, pending_store) -> None:
    """直写去重：该轮已有 turn: 直写条目 → 草稿不得再提名。"""
    episodic, long_term = _seeded(tmp_path, episode_id="dup-1")
    episodic.record(
        replace(episodic.get("dup-1"), user_input="我特别喜欢数字 8 和靛蓝色，记住啦。")
    )
    long_term.store(
        MemoryItem(
            item_id="turn:sess-x:3",
            content="[preference] 用户的偏好：数字 8 和靛蓝色。",
            importance=0.5,
            metadata={"user_input": "我特别喜欢数字 8 和靛蓝色，记住啦。"},
        )
    )

    report = consolidate_episodes(episodic, long_term)
    assert report.staged == [], "已有直写覆盖的回合不得再提名草稿"
    assert pending_store.list_pending() == []


def test_unrelated_direct_write_does_not_block(tmp_path, pending_store) -> None:
    """防过度拦截：直写条目来源回合与该情节不同 → 照常提名。"""
    episodic, long_term = _seeded(tmp_path, episode_id="dup-2")
    episodic.record(replace(episodic.get("dup-2"), user_input="新的问题"))
    long_term.store(
        MemoryItem(
            item_id="turn:sess-y:1",
            content="[business_fact] 别的内容。",
            importance=0.5,
            metadata={"user_input": "别的问题"},
        )
    )

    report = consolidate_episodes(episodic, long_term)
    assert report.staged == ["dup-2"]
