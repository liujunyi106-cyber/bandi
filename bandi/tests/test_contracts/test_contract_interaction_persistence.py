"""功能：交互持久化契约——pending 跨重启存活 / 过期清扫 / 孤儿判定 / 写穿一致性。"""

from __future__ import annotations

import asyncio
import time

import pytest

from bandi.core.interaction_persistence import InteractionPersistence
from bandi.core.user_interaction import (
    INTERACTION_KIND_INPUT,
    INTERACTION_KIND_NOTIFY,
    INTERACTION_KIND_PERMISSION,
    PERMISSION_INTERACTION_TTL,
    InteractionStore,
)


def _persistence(tmp_path):
    return InteractionPersistence(tmp_path / "interactions.db")


def test_pending_survives_restart_and_orphan_flag(tmp_path) -> None:
    store = InteractionStore(persistence=_persistence(tmp_path))
    item = store.register(
        session_id="sess-1",
        kind=INTERACTION_KIND_INPUT,
        payload={"prompt": "问题?"},
        ttl_seconds=300,
    )
    assert store.is_orphaned(item) is False

    revived_store = InteractionStore(persistence=_persistence(tmp_path))
    pending = revived_store.list_pending(session_id="sess-1")
    assert [entry.interaction_id for entry in pending] == [item.interaction_id]
    revived = pending[0]
    assert revived.payload == {"prompt": "问题?"}
    assert revived.remaining_seconds() > 0
    assert revived_store.is_orphaned(revived) is True

    assert revived_store.resolve(revived.interaction_id, "答案") is True
    final_store = InteractionStore(persistence=_persistence(tmp_path))
    assert final_store.list_pending(session_id="sess-1") == []


def test_expired_item_swept_on_hydrate(tmp_path) -> None:
    persistence = _persistence(tmp_path)
    store = InteractionStore(persistence=persistence)
    item = store.register(
        session_id="sess-2",
        kind=INTERACTION_KIND_INPUT,
        payload={"prompt": "会过期"},
        ttl_seconds=300,
    )
    item.created_at = time.time() - 10_000
    persistence.upsert(item)

    revived = InteractionStore(persistence=_persistence(tmp_path))
    assert revived.list_pending(session_id="sess-2") == []


def test_permission_ttl_roundtrip(tmp_path) -> None:
    store = InteractionStore(persistence=_persistence(tmp_path))
    item = store.register(
        session_id="sess-3",
        kind=INTERACTION_KIND_PERMISSION,
        payload={"tool_name": "bash", "args": {}, "risk": "high"},
        ttl_seconds=PERMISSION_INTERACTION_TTL,
    )
    revived = InteractionStore(persistence=_persistence(tmp_path))
    pending = revived.list_pending(session_id="sess-3")
    assert [entry.interaction_id for entry in pending] == [item.interaction_id]
    assert pending[0].ttl_seconds == PERMISSION_INTERACTION_TTL


@pytest.mark.asyncio
async def test_same_process_resolve_still_wakes_future(tmp_path) -> None:
    store = InteractionStore(persistence=_persistence(tmp_path))
    item = store.register(
        session_id="sess-4",
        kind=INTERACTION_KIND_INPUT,
        payload={"prompt": "同进程"},
        ttl_seconds=30,
    )
    task = asyncio.create_task(store.get_decision(item.interaction_id))
    await asyncio.sleep(0)
    assert store.resolve(item.interaction_id, "ok") is True
    assert await task == "ok"


def test_notify_rejected_not_rehydrated(tmp_path) -> None:
    store = InteractionStore(persistence=_persistence(tmp_path))
    item = store.register(
        session_id="sess-5",
        kind=INTERACTION_KIND_NOTIFY,
        payload={"message": "仅提示"},
    )
    assert store.reject(item.interaction_id) is True
    revived = InteractionStore(persistence=_persistence(tmp_path))
    assert revived.list_pending(session_id="sess-5") == []


def test_memory_only_store_unchanged(tmp_path) -> None:
    store = InteractionStore()
    item = store.register(
        session_id="sess-6",
        kind=INTERACTION_KIND_INPUT,
        payload={"prompt": "内存态"},
        ttl_seconds=30,
    )
    assert store.is_orphaned(item) is False
    assert store.resolve(item.interaction_id, "x") is True
    fresh = InteractionStore()
    assert fresh.list_pending(session_id="sess-6") == []
