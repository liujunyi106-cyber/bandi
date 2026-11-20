"""功能：压缩必须真正「减行」，而不只是追加。

真机缺陷：`compact_session` 只做三次 append（summary + preserved_segment +
compaction_card），`get_branch` 靠**读时切片**掩盖旧消息——主表行数只增不减。
连续评测 80 条后单会话累积 3353 条（`session_entries` 里 checkpoint 占 64%）。

本契约钉两件事：
1. `archive_entries_before()` 把 summary 之前的历史移出主表、进归档表；
2. 归档后 `get_branch()` 历史不丢（摘要仍在、归档可回溯）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.message import Message, MessageRole
from bandi.memory.session.store import SessionStore


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "s.db"))


def _seed(store: SessionStore, session_id: str, turns: int) -> None:
    """功能：写入 turns 轮 user/assistant 消息，每轮后追加一条 summary 边界。"""
    for i in range(turns):
        store.append_message(
            session_id, Message(role=MessageRole.USER, content=f"u{i}", turn_index=i)
        )
        store.append_message(
            session_id, Message(role=MessageRole.ASSISTANT, content=f"a{i}", turn_index=i)
        )
        store.append_summary_entry(session_id, i, f"summary-{i}")


def test_archive_moves_history_out_of_main_table(store: SessionStore) -> None:
    """功能：归档必须真的从主表删行（不是只加摘要）。"""
    sid = store.create_session(title="t")
    _seed(store, sid, turns=6)
    before = store.count_entries(sid)
    assert before > 0

    archived = store.archive_entries_before(sid, keep_latest=1)

    after = store.count_entries(sid)
    assert archived > 0, "有 summary 边界时应归档出内容"
    assert after == before - archived, (
        f"归档必须减行：before={before} after={after} archived={archived}"
    )
    assert after < before


def test_archive_does_not_change_visible_branch(store: SessionStore) -> None:
    """功能：归档只动被摘要覆盖的旧行，不得改变可见分支。

    `get_branch` 本就按最近 summary 切片只返回边界之后的内容——
    归档剔除的正是已被屏蔽的前缀，所以对外可见分支应一字不变。
    """
    sid = store.create_session(title="t")
    _seed(store, sid, turns=4)
    before = [str(m.get("content") or "") for m in store.get_branch(sid)]

    store.archive_entries_before(sid, keep_latest=1)

    after = [str(m.get("content") or "") for m in store.get_branch(sid)]
    assert after == before, f"归档不得改变可见分支：before={before} after={after}"
    assert any(c.startswith("summary-") for c in after), "摘要必须仍在"


def test_archive_is_noop_without_boundary(store: SessionStore) -> None:
    """功能：没有 summary 边界时不得归档（避免把完整历史清空）。"""
    sid = store.create_session(title="t")
    store.append_message(sid, Message(role=MessageRole.USER, content="only"))
    before = store.count_entries(sid)

    archived = store.archive_entries_before(sid)

    assert archived == 0
    assert store.count_entries(sid) == before


def test_archived_payload_is_recoverable(store: SessionStore) -> None:
    """功能：归档表必须留存完整行（可审计可回溯），不是丢弃。"""
    sid = store.create_session(title="t")
    _seed(store, sid, turns=5)
    store.archive_entries_before(sid, keep_latest=1)

    with store._lock:  # noqa: SLF001 — 契约需直接核对归档表内容
        rows = store._sqlite.execute(
            "SELECT payload FROM session_entries_archive WHERE session_id = ?", (sid,)
        ).fetchall()
    assert rows, "归档表应有记录"
    import json as _json

    payload = _json.loads(rows[0][0])
    assert payload.get("content"), "归档 payload 必须含原始内容"
    assert "entry_type" in payload and "created_at" in payload
