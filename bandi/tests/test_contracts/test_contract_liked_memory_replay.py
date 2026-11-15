# 功能：契约——点赞提名必须能被批准重放为长期记忆。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class _PendingStore:
    def __init__(self) -> None:
        self.staged: list[dict[str, Any]] = []

    def list_pending(self) -> list[Any]:
        return []

    def stage(self, payload: dict[str, Any]) -> Any:
        self.staged.append(payload)
        return SimpleNamespace(id8="liked-1")


class _LongTerm:
    def __init__(self) -> None:
        self.stored: list[tuple[Any, dict[str, Any]]] = []

    def store(self, item: Any, **kwargs: Any) -> None:
        self.stored.append((item, kwargs))


@pytest.mark.asyncio
async def test_liked_memory_payload_replays_into_long_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    app_module = importlib.import_module("bandi.api.app")
    from bandi.memory.staging import replay_record

    store = _PendingStore()
    monkeypatch.setattr(app_module, "_get_memory_pending_store", lambda: store)

    record_id = await app_module._nominate_liked_memory(
        "session-1",
        "entry-1",
        {"content": "客户偏好周五下午沟通", "project_id": "proj-1"},
    )

    assert record_id == "liked-1"
    payload = store.staged[0]["payload"]
    assert payload["target"] == "long_term", "缺少 target，replay_record 会直接返回 False"
    assert payload["item"]["content"] == "客户偏好周五下午沟通"
    assert payload["project_id"] == "proj-1"

    long_term = _LongTerm()
    assert replay_record(SimpleNamespace(payload=payload), long_term=long_term) is True
    assert len(long_term.stored) == 1
    item, scope = long_term.stored[0]
    assert item.content == "客户偏好周五下午沟通"
    assert scope.get("project_id") == "proj-1"
