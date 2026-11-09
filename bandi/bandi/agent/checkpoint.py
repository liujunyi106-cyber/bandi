# 功能：Agent 检查点持久化（CheckpointStore 保存/加载/按会话列表）
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

MAX_CHECKPOINTS_PER_SESSION = 100

_CHECKPOINT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS checkpoints (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        turn_index INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        messages_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        command_id TEXT NOT NULL DEFAULT '',
        UNIQUE(session_id, turn_index)
    )
"""


@dataclass
class Checkpoint:
    id: str = field(default_factory=lambda: uuid4().hex)
    session_id: str = ""
    turn_index: int = 0
    state_json: str = ""
    messages_json: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    command_id: str = ""


class CheckpointStore:
    def __init__(self, db_path: str | None = None, conn: sqlite3.Connection | None = None) -> None:
        # 外部连接注入（SessionStore 同库原子提交预留）；缺省自建连接 + WAL。
        # 默认路径在**运行时**经 Path.home() 求值（默认参数在 import 期求值会
        # 绕过测试的 Path.home patch，命中真实用户库造成跨运行状态污染）。
        if conn is not None:
            self._db = conn
            self._owns_conn = False
        else:
            resolved = db_path or str(Path.home() / ".bandi" / "checkpoints.db")
            db_path = str(Path(resolved).expanduser())
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = connect_app_db(db_path, timeout=30)
            self._owns_conn = True
        self._init_db()

    def _init_db(self) -> None:
        self._db.execute(_CHECKPOINT_SCHEMA)
        self._db.commit()
        # 旧库迁移：无 command_id 列（新 schema 特征）→ 去重重建
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(checkpoints)").fetchall()]
        if "command_id" not in cols:
            self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        # 功能：旧库（id 主键无唯一约束）迁移——每 (session,turn) 保留最新行后重建
        try:
            self._db.execute("""
                DELETE FROM checkpoints WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY session_id, turn_index
                                   ORDER BY created_at DESC, id DESC
                               ) AS rn
                        FROM checkpoints
                    ) WHERE rn = 1
                )
            """)
            self._db.execute("ALTER TABLE checkpoints RENAME TO checkpoints_legacy")
            self._db.execute(_CHECKPOINT_SCHEMA)
            self._db.execute("""
                INSERT INTO checkpoints
                    (id, session_id, turn_index, state_json, messages_json, created_at)
                SELECT id, session_id, turn_index, state_json, messages_json, created_at
                FROM checkpoints_legacy
            """)
            self._db.execute("DROP TABLE checkpoints_legacy")
            self._db.commit()
        except sqlite3.OperationalError as e:
            logger.warning("checkpoint legacy migration skipped (idempotent re-entry): %s", e)
            self._db.rollback()

    def save(self, cp: Checkpoint) -> None:
        # 功能：按 (session_id, turn_index) upsert——同键覆盖最新（每 turn 恒一条
        # _owns_conn=False 不 commit，另一连接不可见）
        with self._db:
            self._db.execute("DELETE FROM checkpoints WHERE id = ?", (cp.id,))
            self._db.execute(
                "DELETE FROM checkpoints WHERE session_id = ? AND turn_index = ?",
                (cp.session_id, cp.turn_index),
            )
            self._db.execute(
                """INSERT INTO checkpoints
                       (id, session_id, turn_index, state_json, messages_json, created_at, command_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    cp.id,
                    cp.session_id,
                    cp.turn_index,
                    cp.state_json,
                    cp.messages_json,
                    cp.created_at,
                    cp.command_id,
                ),
            )
            self._db.execute(
                """DELETE FROM checkpoints WHERE session_id = ? AND id NOT IN (
                       SELECT id FROM checkpoints WHERE session_id = ?
                       ORDER BY turn_index DESC LIMIT ?
                   )""",
                (cp.session_id, cp.session_id, MAX_CHECKPOINTS_PER_SESSION),
            )

    def load(self, id: str) -> Checkpoint | None:
        row = self._db.execute("SELECT * FROM checkpoints WHERE id=?", (id,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self._db.execute("SELECT * FROM checkpoints LIMIT 0").description]
        return Checkpoint(
            id=row[cols.index("id")],
            session_id=row[cols.index("session_id")],
            turn_index=row[cols.index("turn_index")],
            state_json=row[cols.index("state_json")],
            messages_json=row[cols.index("messages_json")],
            created_at=row[cols.index("created_at")],
            command_id=row[cols.index("command_id")] if "command_id" in cols else "",
        )

    def list_by_session(self, session_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, turn_index, created_at FROM checkpoints "
            "WHERE session_id=? ORDER BY turn_index, created_at",
            (session_id,),
        ).fetchall()
        return [{"id": r[0], "turn_index": r[1], "created_at": r[2]} for r in rows]

    def delete_by_session(self, session_id: str) -> None:
        """删除某 session 全部 checkpoint（子 Agent 失败清理残留快照）。"""
        self._db.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
        self._db.commit()

    def close(self) -> None:
        if self._owns_conn:
            self._db.close()
