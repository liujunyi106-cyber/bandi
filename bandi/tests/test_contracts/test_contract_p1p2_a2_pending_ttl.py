"""契约：审批 pending 有 TTL（过期作废）且队首淘汰不再静默。

旧行为：pending 只写 created_at 从不读，永不失效；超过 5 条直接 pop(0)，无日志无事件。
现行为：读取/入队时清理超 TTL 条目并落库；队首淘汰写 warning（含被淘汰 tool_call_id）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from bandi.tools.approval import _PENDING_MAX_PER_SESSION, ApprovalStore, PendingApproval


def _pending(call_id: str, *, age_seconds: float = 0.0) -> PendingApproval:
    return PendingApproval(
        tool_call_id=call_id,
        tool_name="bash",
        params_digest="digest",
        reason="needs approval",
        created_at=time.time() - age_seconds,
    )


def test_expired_pending_is_dropped_on_read(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.json")
    store.set_pending("s1", _pending("old", age_seconds=4000))
    store.set_pending("s1", _pending("fresh"))

    assert [p.tool_call_id for p in store.list_pending("s1")] == ["fresh"]
    first = store.get_pending("s1")
    assert first is not None and first.tool_call_id == "fresh"
    assert [item.tool_call_id for _sid, item in store.list_all_pending()] == ["fresh"]


def test_expired_pending_survives_no_reload_and_is_persisted_out(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    store = ApprovalStore(path)
    store.set_pending("s1", _pending("old", age_seconds=4000))
    store.set_pending("s1", _pending("fresh"))
    store.list_pending("s1")

    reloaded = ApprovalStore(path)
    assert [p.tool_call_id for p in reloaded.list_pending("s1")] == ["fresh"]


def test_pop_expired_pending_returns_none(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "approvals.json")
    store.set_pending("s1", _pending("old", age_seconds=4000))

    assert store.pop_pending("s1") is None
    assert store.pop_pending("s1", tool_call_id="old") is None


def test_pending_cap_eviction_is_logged(tmp_path: Path, caplog) -> None:
    store = ApprovalStore(tmp_path / "approvals.json")
    with caplog.at_level(logging.WARNING, logger="bandi.tools.approval"):
        for index in range(_PENDING_MAX_PER_SESSION + 1):
            store.set_pending("s1", _pending(f"c{index}"))

    kept = [p.tool_call_id for p in store.list_pending("s1")]
    assert len(kept) == _PENDING_MAX_PER_SESSION
    assert "c0" not in kept, "超上限应淘汰队首"
    messages = [record.getMessage() for record in caplog.records]
    assert any("evicted oldest" in message for message in messages), (
        f"淘汰必须有日志，实际 {messages}"
    )
