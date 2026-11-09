# 功能：存储层时间口径契约——写入 UTC 带偏移、历史本地 naive 迁移、排序与接口出口一致

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from bandi.company.store import WorkItemStore
from bandi.memory.session.store import SessionStore
from bandi.utils.timefmt import iso_local, migrate_naive_timestamps

_SESSIONS_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    mode TEXT DEFAULT 'execute',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    status TEXT DEFAULT 'active'
)
"""

_JOURNAL_DDL = """
CREATE TABLE work_item_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_item_id TEXT NOT NULL,
    session_id TEXT DEFAULT '',
    event TEXT NOT NULL,
    detail TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
)
"""


def _seed(db_path: Path, ddl: str, insert_sql: str, values: tuple[object, ...]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(ddl)
    conn.execute(insert_sql, values)
    conn.commit()
    conn.close()


def _is_utc_aware(text: str) -> bool:
    parsed = datetime.fromisoformat(text)
    return parsed.tzinfo is not None and parsed.utcoffset() == datetime.now(UTC).utcoffset()


class TestUtcWrites:
    def test_new_session_timestamps_are_utc_aware(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path / "s.db")
        sid = store.create_session(title="新会话")
        row = store.get_session(sid)
        assert row is not None
        assert _is_utc_aware(row["created_at"])
        assert _is_utc_aware(row["updated_at"])
        delta = (datetime.fromisoformat(row["created_at"]) - datetime.now(UTC)).total_seconds()
        assert abs(delta) < 10

    def test_journal_timestamp_is_utc_aware(self, tmp_path: Path) -> None:
        store = WorkItemStore(str(tmp_path / "w.db"))
        store.record_journal("wi-1", "created")
        entries = store.list_journal("wi-1")
        assert entries and _is_utc_aware(entries[0]["created_at"])


class TestLegacyMigration:
    def test_naive_rows_rewritten_to_utc_same_instant(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        naive = "2026-10-31T13:45:12.123456"
        _seed(
            db_path,
            _SESSIONS_DDL,
            "INSERT INTO sessions (id, title, mode, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("s-old", "旧会话", "execute", naive, naive),
        )
        store = SessionStore(db_path)
        row = store.get_session("s-old")
        assert row is not None
        migrated = datetime.fromisoformat(row["created_at"])
        assert migrated.tzinfo is not None
        assert migrated == datetime.fromisoformat(naive).astimezone(UTC)
        store.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy2.db"
        naive = "2026-10-31T08:00:00"
        _seed(
            db_path,
            _SESSIONS_DDL,
            "INSERT INTO sessions (id, title, mode, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("s-old", "旧会话", "execute", naive, naive),
        )
        first = SessionStore(db_path)
        after_first = first.get_session("s-old")["updated_at"]
        first.close()
        second = SessionStore(db_path)
        after_second = second.get_session("s-old")["updated_at"]
        second.close()
        assert after_first == after_second

    def test_migration_skips_aware_values(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy3.db"
        aware = "2026-10-31T05:45:12.123456+00:00"
        _seed(
            db_path,
            _SESSIONS_DDL,
            "INSERT INTO sessions (id, title, mode, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("s-new", "新会话", "execute", aware, aware),
        )
        store = SessionStore(db_path)
        assert store.get_session("s-new")["created_at"] == aware
        store.close()

    def test_ordering_correct_across_migration_boundary(self, tmp_path: Path) -> None:
        """迁移后，旧行（本地 13:45=UTC 05:45）应早于新行（UTC 06:00）。"""
        db_path = tmp_path / "order.db"
        _seed(
            db_path,
            _SESSIONS_DDL,
            "INSERT INTO sessions (id, title, mode, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("s-old", "旧", "execute", "2026-10-31T13:45:12", "2026-10-31T13:45:12"),
        )
        store = SessionStore(db_path)
        store.create_session(title="新")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = 's-old'",
            (datetime.fromisoformat("2026-10-31T05:45:12").astimezone(UTC).isoformat(),),
        )
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE title = '新'",
            (datetime.fromisoformat("2026-10-31T06:00:00").astimezone(UTC).isoformat(),),
        )
        conn.commit()
        conn.close()
        titles = [row["title"] for row in store.list_sessions()]
        assert titles.index("新") < titles.index("旧")
        store.close()

    def test_journal_legacy_row_migrated(self, tmp_path: Path) -> None:
        db_path = tmp_path / "wlegacy.db"
        _seed(
            db_path,
            _JOURNAL_DDL,
            "INSERT INTO work_item_journal (work_item_id, session_id, event, detail, created_at) "
            "VALUES (?,?,?,?,?)",
            ("wi-1", "", "created", "{}", "2026-10-31T13:45:12"),
        )
        store = WorkItemStore(str(db_path))
        entries = store.list_journal("wi-1")
        assert _is_utc_aware(entries[0]["created_at"])


class TestApiExit:
    def test_iso_local_restores_original_local_instant(self, tmp_path: Path) -> None:
        db_path = tmp_path / "exit.db"
        naive = "2026-10-31T13:45:12"
        _seed(
            db_path,
            _SESSIONS_DDL,
            "INSERT INTO sessions (id, title, mode, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("s-old", "旧", "execute", naive, naive),
        )
        store = SessionStore(db_path)
        stored = store.get_session("s-old")["created_at"]
        store.close()
        expected = datetime.fromisoformat(naive).astimezone()
        assert datetime.fromisoformat(iso_local(stored)) == expected


class TestMigratorUnit:
    def test_returns_zero_when_no_naive_values(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "m.db")
        conn.execute(_SESSIONS_DDL)
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            ("s", "t", "2026-10-31T05:45:12+00:00", "2026-10-31T05:45:12+00:00"),
        )
        assert migrate_naive_timestamps(conn, {"sessions": ("created_at", "updated_at")}) == 0
        conn.close()

    def test_space_separated_legacy_value_migrated(self, tmp_path: Path) -> None:
        """空格分隔的历史串（`YYYY-MM-DD HH:MM:SS`）同样归一为 UTC 带偏移。"""
        conn = sqlite3.connect(tmp_path / "m4.db")
        conn.execute(_SESSIONS_DDL)
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            ("s", "t", "2026-10-31 13:45:12", "2026-10-31 13:45:12"),
        )
        assert migrate_naive_timestamps(conn, {"sessions": ("created_at", "updated_at")}) == 2
        value = conn.execute("SELECT created_at FROM sessions").fetchone()[0]
        assert datetime.fromisoformat(value) == datetime.fromisoformat(
            "2026-10-31 13:45:12"
        ).astimezone(UTC)
        conn.close()

    def test_counts_rewritten_cells(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "m2.db")
        conn.execute(_SESSIONS_DDL)
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            ("s", "t", "2026-10-31T13:45:12", "2026-10-31T13:46:12"),
        )
        assert migrate_naive_timestamps(conn, {"sessions": ("created_at", "updated_at")}) == 2
        conn.close()

    def test_missing_table_is_tolerated(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(tmp_path / "m3.db")
        assert migrate_naive_timestamps(conn, {"nope": ("created_at",)}) == 0
        conn.close()
