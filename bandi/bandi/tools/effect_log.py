# 功能：工具副作用日志——每次工具调用落一条 started/succeeded/replayed/failed，
# 供崩溃后判定「哪些步骤真的执行过」（有 started 无终态 = 可疑步骤）
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_effects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    turn_index INTEGER NOT NULL DEFAULT 0,
    step_key TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL DEFAULT '',
    args_hash TEXT NOT NULL DEFAULT '',
    read_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effects_session ON tool_effects(session_id, turn_index);
CREATE INDEX IF NOT EXISTS idx_effects_step ON tool_effects(step_key);
"""

STATUS_STARTED = "started"
STATUS_SUCCEEDED = "succeeded"
STATUS_REPLAYED = "replayed"


def default_effect_log_path() -> Path:
    # 功能：副作用日志默认库路径（与其它 bandi 数据同目录）
    return Path.home() / ".bandi" / "data" / "tool_effects.db"


class EffectLog:
    """# 功能：副作用日志存储——写入 best-effort，查询用于崩溃后对齐执行进度。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else default_effect_log_path()
        self._lock = threading.Lock()
        self._ready = False
        self._queue: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=2000)
        self._writer = threading.Thread(target=self._drain, name="effect-log", daemon=True)
        self._writer.start()

    def _connect(self) -> sqlite3.Connection:
        from bandi.utils.db import connect

        self._path.parent.mkdir(parents=True, exist_ok=True)
        return connect(self._path, wal=True, timeout=3)

    def _ensure(self, conn: sqlite3.Connection) -> None:
        if not self._ready:
            conn.executescript(_SCHEMA)
            self._ready = True

    def record(
        self,
        *,
        session_id: str = "",
        turn_index: int = 0,
        step_key: str = "",
        tool_name: str = "",
        args_hash: str = "",
        read_only: bool = False,
        status: str = "",
        error: str = "",
    ) -> None:
        """功能：追加一条工具调用记录（入队即返回，写盘由后台线程完成，不阻塞主链路）。"""
        try:
            self._queue.put_nowait(
                (
                    str(session_id or ""),
                    int(turn_index or 0),
                    str(step_key or ""),
                    str(tool_name or ""),
                    str(args_hash or ""),
                    bool(read_only),
                    str(status or ""),
                    str(error or ""),
                )
            )
        except queue.Full:
            logger.debug("effect log queue full; record dropped")

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            try:
                self._write(*item)
            finally:
                self._queue.task_done()

    def _write(
        self,
        session_id: str,
        turn_index: int,
        step_key: str,
        tool_name: str,
        args_hash: str,
        read_only: bool,
        status: str,
        error: str,
    ) -> None:
        try:
            with self._lock, self._connect() as conn:
                self._ensure(conn)
                conn.execute(
                    "INSERT INTO tool_effects"
                    "(session_id, turn_index, step_key, tool_name, args_hash, read_only,"
                    " status, error, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        turn_index,
                        step_key,
                        tool_name,
                        args_hash,
                        1 if read_only else 0,
                        status,
                        error[:500],
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except Exception as e:  # noqa: BLE001 — 日志失败不影响工具执行
            logger.debug("effect log record failed: %s", e)

    def pending(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """功能：列出「started 却无终态」的步骤——崩溃/中断后的可恢复线索。"""
        self._queue.join()
        try:
            with self._connect() as conn:
                self._ensure(conn)
                rows = conn.execute(
                    "SELECT step_key, tool_name, args_hash, turn_index, created_at"
                    " FROM tool_effects WHERE session_id=? AND status=?"
                    " AND step_key NOT IN ("
                    "   SELECT step_key FROM tool_effects WHERE session_id=? AND status IN (?,?)"
                    " ) ORDER BY id DESC LIMIT ?",
                    (
                        str(session_id or ""),
                        STATUS_STARTED,
                        str(session_id or ""),
                        STATUS_SUCCEEDED,
                        STATUS_REPLAYED,
                        int(limit),
                    ),
                ).fetchall()
                return [
                    {
                        "step_key": r[0],
                        "tool_name": r[1],
                        "args_hash": r[2],
                        "turn_index": r[3],
                        "created_at": r[4],
                    }
                    for r in rows
                ]
        except Exception as e:  # noqa: BLE001 — 查询失败按空处理
            logger.debug("effect log pending failed: %s", e)
            return []

    def count(self, session_id: str = "") -> int:
        self._queue.join()
        try:
            with self._connect() as conn:
                self._ensure(conn)
                if session_id:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM tool_effects WHERE session_id=?", (session_id,)
                    ).fetchone()
                else:
                    row = conn.execute("SELECT COUNT(*) FROM tool_effects").fetchone()
                return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0


_LOG: EffectLog | None = None
_LOG_LOCK = threading.Lock()


def get_effect_log() -> EffectLog:
    # 功能：进程级副作用日志单例（懒建；测试可替换模块级 _LOG）
    global _LOG
    if _LOG is None:
        with _LOG_LOCK:
            if _LOG is None:
                _LOG = EffectLog()
    return _LOG
