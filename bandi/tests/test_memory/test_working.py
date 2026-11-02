from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from bandi.memory.working import ScratchpadStore, ToolInvocation


@pytest.fixture()
def store(tmp_path: Any) -> ScratchpadStore:
    return ScratchpadStore(str(tmp_path / "working.db"))


def test_set_get_roundtrip(store: ScratchpadStore) -> None:
    store.set("goal", "完成数据迁移脚本", session_id="s1")
    assert store.get("goal", session_id="s1") == "完成数据迁移脚本"


def test_get_missing_key_returns_none(store: ScratchpadStore) -> None:
    assert store.get("nope", session_id="s1") is None


def test_all_returns_only_requested_session(store: ScratchpadStore) -> None:
    store.set("a", "甲", session_id="s1")
    store.set("b", "乙", session_id="s2")
    assert store.all(session_id="s1") == {"a": "甲"}


def test_clear_removes_session_scope_only(store: ScratchpadStore) -> None:
    store.set("k", "v1", session_id="s1")
    store.set("k", "v2", session_id="s2")

    removed = store.clear(session_id="s1")

    assert removed == 1
    assert store.get("k", session_id="s1") is None
    assert store.get("k", session_id="s2") == "v2"


def test_search_chinese_hits_kv_and_tool_preview(store: ScratchpadStore) -> None:
    store.set("turn:0", "Redis 缓存穿透排查结论：空值缓存兜底", session_id="s1")
    store.append_tool(
        ToolInvocation(
            name="bash", result_preview="redis-cli 检测缓存穿透 输出 PONG", session_id="s1"
        )
    )

    hits = store.search("缓存穿透", session_id="s1")

    values = [entry.value for entry, _ in hits]
    assert any("空值缓存兜底" in v for v in values), f"必须命中 KV（实际 {values}）"
    assert any("PONG" in v for v in values), f"必须命中工具历史（实际 {values}）"


def test_search_empty_query_returns_empty(store: ScratchpadStore) -> None:
    store.set("k", "内容", session_id="s1")
    assert store.search("   ", session_id="s1") == []


def test_count_counts_kv_entries(store: ScratchpadStore) -> None:
    assert store.count() == 0
    store.set("a", "1", session_id="s1")
    store.set("b", "2", session_id="s1")
    assert store.count() == 2


def test_close_releases_connection(tmp_path: Any) -> None:
    store = ScratchpadStore(str(tmp_path / "working.db"))
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.count()


def test_short_ttl_expires_entry(tmp_path: Any) -> None:
    """ttl_seconds=1 时写入的条目在时间推进后过期（公开行为：get 不可见）。"""
    import sqlite3 as _sq
    from datetime import UTC, datetime, timedelta

    store = ScratchpadStore(str(tmp_path / "working.db"), ttl_seconds=1)
    store.set("k", "临时值", session_id="s1")

    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    conn = _sq.connect(str(tmp_path / "working.db"))
    conn.execute("UPDATE scratch_kv SET created_at=?", (past,))
    conn.commit()
    conn.close()

    assert store.get("k", session_id="s1") is None


def test_turn_rows_partial_index_created(store: ScratchpadStore) -> None:
    """契约（🟢-D）：`turn:%` 裁剪不得全表扫描——部分索引随开库建立。"""
    conn = sqlite3.connect(store.db_path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_scratch_kv_turn'"
    ).fetchone()
    conn.close()

    assert row is not None, "scratch_kv 缺少 turn:* 部分索引（P2-7 裁剪将全表扫描）"
    assert "WHERE" in (row[0] or "").upper(), "索引必须是部分索引（只覆盖 turn:% 行）"
