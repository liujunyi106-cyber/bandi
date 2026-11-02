# 功能：UtilStore — cache/cron 共用 SQLite 持久化存储（替换 Redis/ORM，threading.Lock + 写操作 to_thread）
from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db


def default_util_db_path() -> Path:
    """功能：返回默认 util 库路径（惰性解析——防收集期 import 固化真实 home）。"""
    path = Path.home() / ".bandi" / "data" / "util.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _now_ts() -> float:
    return datetime.now(UTC).timestamp()


class UtilStore:
    """功能：cache_entries 单库持久化；调用方持有单例（工具构造时注入）。

    设计：key = f"{namespace}:{key}"；TTL 惰性过期（读命中检查+删）
    increment/decrement 走 int_value 列避免 JSON 竞争读改写；写操作经 to_thread。
    cron 工具面已改接 TaskStore（cron 桥接：存储唯一事实源=调度库）。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = str(db_path or default_util_db_path())
        self._lock = threading.Lock()
        self._sqlite = connect_app_db(self._db_path, isolation_level=None, row_factory=True)
        self._metrics = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletes": 0,
            "total_requests": 0,
            "current_size": 0,
        }
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._lock:
            self._sqlite.execute("""
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY,
                    namespace TEXT DEFAULT 'default',
                    value TEXT NOT NULL,
                    expires_at REAL,
                    access_count INTEGER DEFAULT 0,
                    last_accessed REAL,
                    tags TEXT DEFAULT '[]',
                    is_int INTEGER DEFAULT 0,
                    int_value REAL
                )
            """)
            self._sqlite.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_ns ON cache_entries(namespace)"
            )

    # ------------------------------------------------------------------ cache
    def _sync_get(self, namespace: str, key: str, now: float) -> dict[str, Any] | None:
        with self._lock:
            k = f"{namespace}:{key}"
            row = self._sqlite.execute("SELECT * FROM cache_entries WHERE key = ?", (k,)).fetchone()
            if row is None:
                self._metrics["misses"] += 1
                return None
            if row["expires_at"] is not None and now > row["expires_at"]:
                self._sqlite.execute("DELETE FROM cache_entries WHERE key = ?", (k,))
                self._metrics["misses"] += 1
                return None
            self._sqlite.execute(
                "UPDATE cache_entries SET access_count = access_count + 1, "
                "last_accessed = ? WHERE key = ?",
                (now, k),
            )
            self._metrics["hits"] += 1
            return dict(row)

    def cache_get(self, namespace: str, key: str) -> dict[str, Any] | None:
        self._metrics["total_requests"] += 1
        now = _now_ts()
        row = self._sync_get(namespace, key, now)
        if row is None:
            return None
        value: Any = json.loads(row["value"])
        if row["is_int"]:
            value = row["int_value"] if row["int_value"] is not None else value
        return {
            "value": value,
            "expires_at": row["expires_at"],
            "access_count": row["access_count"],
            "last_accessed": row["last_accessed"],
            "tags": json.loads(row["tags"]),
        }

    def cache_set(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl: float | None = None,
        tags: list[str] | None = None,
    ) -> None:
        def _write() -> None:
            expires_at = _now_ts() + ttl if ttl is not None else None
            is_int = isinstance(value, bool) is False and isinstance(value, int)
            int_value: float | None = float(value) if is_int else None
            k = f"{namespace}:{key}"
            with self._lock:
                self._sqlite.execute(
                    "INSERT OR REPLACE INTO cache_entries "
                    "(key, namespace, value, expires_at, access_count, last_accessed, "
                    "tags, is_int, int_value) VALUES (?,?,?,?,0,NULL,?,?,?)",
                    (
                        k,
                        namespace,
                        json.dumps(value, ensure_ascii=False),
                        expires_at,
                        json.dumps(tags or [], ensure_ascii=False),
                        int(is_int),
                        int_value,
                    ),
                )
                self._metrics["sets"] += 1
                self._metrics["total_requests"] += 1

        return asyncio.get_running_loop().run_in_executor(None, _write)

    def cache_delete(self, namespace: str, key: str) -> bool:
        def _write() -> bool:
            with self._lock:
                cur = self._sqlite.execute(
                    "DELETE FROM cache_entries WHERE key = ?", (f"{namespace}:{key}",)
                )
                if cur.rowcount:
                    self._metrics["deletes"] += 1
                self._metrics["total_requests"] += 1
                return bool(cur.rowcount)

        return asyncio.get_running_loop().run_in_executor(None, _write)

    def cache_exists(self, namespace: str, key: str) -> bool:
        self._metrics["total_requests"] += 1
        return self._sync_get(namespace, key, _now_ts()) is not None

    def cache_clear(self) -> int:
        def _write() -> int:
            with self._lock:
                cur = self._sqlite.execute("DELETE FROM cache_entries")
                if cur.rowcount:
                    self._metrics["deletes"] += cur.rowcount
                self._metrics["total_requests"] += 1
                return cur.rowcount

        return asyncio.get_running_loop().run_in_executor(None, _write)

    def cache_stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._sqlite.execute("SELECT COUNT(*) AS n FROM cache_entries").fetchone()
            metrics = dict(self._metrics)
        metrics["current_size"] = row["n"] if row else 0
        return metrics

    def cache_keys(self, namespace: str = "", pattern: str = "") -> list[str]:
        self._metrics["total_requests"] += 1
        with self._lock:
            sql = "SELECT key FROM cache_entries WHERE 1=1"
            params: list[Any] = []
            if namespace:
                sql += " AND namespace = ?"
                params.append(namespace)
            if pattern:
                escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sql += " AND key LIKE ? ESCAPE '\\'"
                params.append(f"%{escaped}%")
            rows = self._sqlite.execute(sql, params).fetchall()
        now = _now_ts()
        out: list[str] = []
        for row in rows:
            if row["key"].endswith(":"):
                continue
            stored = self._sync_get(*row["key"].split(":", 1), now)
            if stored is None:
                continue
            ns, k = row["key"].split(":", 1)
            if namespace and ns != namespace:
                continue
            if pattern and pattern not in k:
                continue
            out.append(k)
        return out

    def cache_mget(self, namespace: str, keys: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {"found": [], "missing": [], "values": {}}
        for key in keys:
            entry = self.cache_get(namespace, key)
            if entry is None:
                result["missing"].append(key)
            else:
                result["found"].append(key)
                result["values"][key] = entry["value"]
        result["found_count"] = len(result["found"])
        result["missing_count"] = len(result["missing"])
        return result

    def cache_get_by_tag(self, namespace: str, tag: str) -> list[dict[str, Any]]:
        self._metrics["total_requests"] += 1
        with self._lock:
            sql = "SELECT * FROM cache_entries WHERE 1=1"
            params: list[Any] = []
            if namespace:
                sql += " AND namespace = ?"
                params.append(namespace)
            escaped = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            sql += " AND tags LIKE ? ESCAPE '\\'"
            params.append(f'%"{escaped}"%')
            rows = self._sqlite.execute(sql, params).fetchall()
        now = _now_ts()
        entries: list[dict[str, Any]] = []
        for row in rows:
            k = row["key"].split(":", 1)[1] if ":" in row["key"] else row["key"]
            stored = self._sync_get(row["namespace"], k, now)
            if stored is None:
                continue
            tags = json.loads(stored["tags"])
            if namespace and stored["namespace"] != namespace:
                continue
            if tag not in tags:
                continue
            value: Any = json.loads(stored["value"])
            if stored["is_int"]:
                value = stored["int_value"] if stored["int_value"] is not None else value
            entries.append(
                {
                    "key": k,
                    "namespace": stored["namespace"],
                    "value": value,
                    "tags": tags,
                }
            )
        return entries

    def cache_delete_by_tag(self, namespace: str, tag: str) -> int:
        def _write() -> int:
            entries = self.cache_get_by_tag(namespace, tag)
            with self._lock:
                for e in entries:
                    self._sqlite.execute(
                        "DELETE FROM cache_entries WHERE key = ?",
                        (f"{e['namespace']}:{e['key']}",),
                    )
                if entries:
                    self._metrics["deletes"] += len(entries)
                self._metrics["total_requests"] += 1
                return len(entries)

        return asyncio.get_running_loop().run_in_executor(None, _write)

    def cache_touch(self, namespace: str, key: str, ttl: float) -> Any:
        def _write() -> bool:
            now = _now_ts()
            with self._lock:
                cur = self._sqlite.execute(
                    "SELECT key FROM cache_entries WHERE key = ?",
                    (f"{namespace}:{key}",),
                )
                if cur.fetchone() is None:
                    self._metrics["total_requests"] += 1
                    return False
                self._sqlite.execute(
                    "UPDATE cache_entries SET expires_at = ? WHERE key = ?",
                    (now + ttl, f"{namespace}:{key}"),
                )
                self._metrics["total_requests"] += 1
                return True

        return asyncio.get_running_loop().run_in_executor(None, _write)

    def cache_increment(self, namespace: str, key: str, amount: float = 1) -> Any:
        def _run() -> float:
            k = f"{namespace}:{key}"
            now = _now_ts()
            row = self._sync_get(namespace, key, now)
            if row is None:
                expires_at = None
                with self._lock:
                    self._sqlite.execute(
                        "INSERT OR REPLACE INTO cache_entries "
                        "(key, namespace, value, expires_at, access_count, last_accessed, "
                        "tags, is_int, int_value) VALUES (?,?,?,?,0,NULL,?,?,?)",
                        (
                            k,
                            namespace,
                            json.dumps(amount, ensure_ascii=False),
                            expires_at,
                            "[]",
                            1,
                            float(amount),
                        ),
                    )
                    self._metrics["sets"] += 1
                    self._metrics["total_requests"] += 1
                return float(amount)
            if not row["is_int"]:
                raise ValueError(f"cache value is not numeric: {key}")
            with self._lock:
                self._sqlite.execute(
                    "UPDATE cache_entries SET int_value = COALESCE(int_value, 0) + ? WHERE key = ?",
                    (float(amount), k),
                )
                new_value = self._sqlite.execute(
                    "SELECT int_value FROM cache_entries WHERE key = ?", (k,)
                ).fetchone()["int_value"]
                self._metrics["total_requests"] += 1
            return float(new_value)

        return asyncio.get_running_loop().run_in_executor(None, _run)

    def cache_info(self, namespace: str, key: str) -> dict[str, Any] | None:
        now = _now_ts()
        row = self._sync_get(namespace, key, now)
        if row is None:
            return None
        ttl_remaining: float | None = None
        if row["expires_at"] is not None:
            ttl_remaining = max(0.0, row["expires_at"] - now)
        return {
            "key": row["key"].split(":", 1)[1],
            "namespace": row["namespace"],
            "expires_at": row["expires_at"],
            "ttl_remaining": ttl_remaining,
            "access_count": row["access_count"],
            "last_accessed": row["last_accessed"],
            "tags": json.loads(row["tags"]),
        }
