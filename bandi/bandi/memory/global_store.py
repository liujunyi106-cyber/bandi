# 功能：全局记忆存储——SQLite event 事件溯源 + global_memory 键值表

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

GLOBAL_MEMORY_MAX_CHARS = 4000

# 按库路径共享的进程内写锁——MemoryHierarchyManager 每 turn 重建
# 实例锁无法串行化跨实例的全局记忆读-改-写；此锁按库文件路径共享。
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


def store_write_lock(db_path: str | Path) -> threading.RLock:
    """功能：取按库路径共享的进程内写锁（跨 store/manager 实例串行化）。"""
    key = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path).resolve())
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[key] = lock
        return lock


def _safe_json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _escape_like(value: str) -> str:
    """LIKE 通配符转义（\\ % _），配合 SQL ESCAPE '\\\\' 使用。"""
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class GlobalMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        # 进程内写锁（读-改-写调用方持有）；CAS 另由 SQL 条件保证跨进程
        self._write_lock = store_write_lock(db_path)
        self._conn = connect_app_db(self._db_path, row_factory=True)
        self._init_db()

    @property
    def write_lock(self) -> threading.RLock:
        """功能：全局记忆读-改-写串行化锁（append_memory CAS 循环持此锁）。"""
        return self._write_lock

    def _init_db(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_sequence (
                event_id TEXT PRIMARY KEY,
                seq INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS global_memory (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # 全局记忆版本表（CAS 防并发丢更新；缺行视为版本 0）
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS global_memory_version (
                key TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, seq)")
        self._conn.commit()

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
    ) -> int:
        """追加事件；event_id 相同则幂等返回已有 seq，不重复写入。

        同族修复：查重 + 插入 + 序号登记必须在同一 `BEGIN IMMEDIATE` 写事务内
        （原 SELECT→INSERT 的 check 窗口无写锁，跨 store 实例/跨进程可双插同 id；
        对齐 `put_global_memory_cas` 的原子模式）。
        """
        with self._lock:
            self._begin_immediate()
            try:
                if event_id is not None:
                    cursor = self._conn.execute(
                        "SELECT seq FROM event_sequence WHERE event_id = ?", (event_id,)
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        self._conn.rollback()
                        return int(existing["seq"])
                cursor = self._conn.execute(
                    "INSERT INTO events (event_type, payload, created_at) VALUES (?, ?, ?)",
                    (event_type, json.dumps(payload or {}, ensure_ascii=False), _now()),
                )
                seq = int(cursor.lastrowid)
                resolved_id = event_id if event_id is not None else f"{event_type}:{seq}"
                self._conn.execute(
                    "INSERT OR REPLACE INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (resolved_id, seq),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return seq

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT seq, event_type, payload, created_at FROM events ORDER BY seq DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
        return [self._row_to_event(row) for row in reversed(rows)]

    def query_events(self, event_type: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT seq, event_type, payload, created_at FROM events "
                "WHERE event_type = ? ORDER BY seq DESC LIMIT ?",
                (event_type, limit),
            )
            rows = cursor.fetchall()
        return [self._row_to_event(row) for row in reversed(rows)]

    @staticmethod
    def _prepare_global_value(key: str, value: Any) -> str:
        """功能：值编码 + 容量约束——返回合法 JSON 字符串（超限非字符串显式拒绝）。

        字符串分支截断方向为**保头**（前缀是直接写入方的身份/定位信息
        见 `test_global_store.py::test_oversized_with_quotes_roundtrips_valid`）。
        受控合并路径（`MemoryHierarchyManager._merge_global_shared`）在写入前按
        **JSON 编码后长度**保尾裁剪到容量内，从源头避免落到本函数的二次头截断
        （截头会丢最新条目并在结尾留下半个条目的碎片）。
        """
        original_len = len(value) if isinstance(value, str) else 0
        if isinstance(value, str):
            value = value[:GLOBAL_MEMORY_MAX_CHARS]
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > GLOBAL_MEMORY_MAX_CHARS:
            # 硬截断会切在 JSON 中间导致读回损坏——循环截断内容直到
            if isinstance(value, str):
                limit = GLOBAL_MEMORY_MAX_CHARS - 4
                value = value[:limit]
                while len(json.dumps(value, ensure_ascii=False)) > GLOBAL_MEMORY_MAX_CHARS:
                    value = value[: max(1, len(value) - 16)]
                encoded = json.dumps(value, ensure_ascii=False)
                logger.warning(
                    "global memory truncated: key=%s chars %d -> %d (cap=%d)",
                    key,
                    original_len,
                    len(value),
                    GLOBAL_MEMORY_MAX_CHARS,
                )
            else:
                # 非字符串值超限不得 str() 破坏类型（读回 dict→str）——
                # 拒绝写入，调用方应显式控制大小
                raise ValueError(
                    f"global memory value for {key!r} exceeds "
                    f"{GLOBAL_MEMORY_MAX_CHARS} chars after JSON encoding; "
                    "non-string values cannot be truncated without breaking their type"
                )
        return encoded

    def _begin_immediate(self) -> None:
        """功能：开启写事务（BEGIN IMMEDIATE）——写锁在事务起点获取，check+write
        期间其他连接（含跨进程）无法插入写，杜绝 CAS 的 TOCTOU 丢更新。"""
        self._conn.execute("BEGIN IMMEDIATE")

    def put_global_memory(self, key: str, value: Any) -> None:
        encoded = self._prepare_global_value(key, value)
        with self._lock:
            # 修复：普通写（版本 bump + 内容更新）同一写事务，防版本回退
            self._begin_immediate()
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO global_memory (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, encoded, _now()),
                )
                self._conn.execute(
                    "INSERT INTO global_memory_version (key, version) VALUES (?, 1) "
                    "ON CONFLICT(key) DO UPDATE SET version = version + 1",
                    (key,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_global_memory_version(self, key: str) -> int:
        """功能：读取全局记忆键版本（缺行 = 0）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM global_memory_version WHERE key = ?", (key,)
            ).fetchone()
        return int(row["version"]) if row is not None else 0

    def put_global_memory_cas(self, key: str, value: Any, expected_version: int) -> bool:
        """功能：版本 CAS 写入——版本不符则拒绝并返回 False（并发丢更新防线）。

        修复：check+write 在同一 BEGIN IMMEDIATE 事务内——原实现读版本
        不开事务、Python 比较后才写，跨进程可丢更新且版本可回退。
        """
        encoded = self._prepare_global_value(key, value)
        with self._lock:
            self._begin_immediate()
            try:
                row = self._conn.execute(
                    "SELECT version FROM global_memory_version WHERE key = ?", (key,)
                ).fetchone()
                current = int(row["version"]) if row is not None else 0
                if current != int(expected_version):
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    "INSERT OR REPLACE INTO global_memory (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    (key, encoded, _now()),
                )
                self._conn.execute(
                    "INSERT INTO global_memory_version (key, version) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET version = excluded.version",
                    (key, current + 1),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def get(self, key: str) -> Any:
        with self._lock:
            cursor = self._conn.execute("SELECT value FROM global_memory WHERE key = ?", (key,))
            row = cursor.fetchone()
        if row is None:
            return None
        return _safe_json_load(row["value"], row["value"])

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            # 未转义时 `50%` 匹配任意前缀、`_` 匹配任意单字符（检索失真）
            escaped = _escape_like(query)
            cursor = self._conn.execute(
                "SELECT key, value, updated_at FROM global_memory "
                "WHERE value LIKE ? ESCAPE '\\' OR key LIKE ? ESCAPE '\\'",
                (f"%{escaped}%", f"%{escaped}%"),
            )
            rows = cursor.fetchall()
        # 旧本地 naive / 新 UTC ISO 混排时颠倒）
        ordered = sorted(rows, key=lambda r: _parse_ts(r["updated_at"]), reverse=True)[:limit]
        return [
            {"key": row["key"], "value": _safe_json_load(row["value"], row["value"])}
            for row in ordered
        ]

    def count(self) -> int:
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM global_memory")
            return int(cursor.fetchone()[0])

    def delete(self, key: str) -> bool:
        """功能：显式遗忘全局记忆键（4.3——此前只有覆盖写，无删除终端）。

        返回是否命中（未命中不报错）。
        """
        k = (key or "").strip()
        if not k:
            return False
        with self._lock:
            cursor = self._conn.execute("DELETE FROM global_memory WHERE key = ?", (k,))
            self._conn.commit()
        return cursor.rowcount > 0

    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "event_type": row["event_type"],
            "payload": _safe_json_load(row["payload"], {}),
            "created_at": row["created_at"],
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _now() -> str:
    # 原 B5c UTC 口径回退；存量 UTC 值由 _parse_ts 归一化排序兼容
    return datetime.now().isoformat()


def _parse_ts(value: str) -> datetime:
    # 功能：RF-10——时间戳归一化（旧本地 naive 与新 UTC ISO 混排时字符串
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(UTC)
