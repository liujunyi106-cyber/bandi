# 功能：UIStateStore — SQLite 持久化 CLI 状态（mode/model），跨 session 恢复

from __future__ import annotations

import threading
from contextlib import closing
from pathlib import Path

from bandi.utils.db import connect


class UIStateStore:
    """持久化 CLI 用户界面状态：mode / model。

    替代每次重启时重新读取 config.yaml 来获取上次的模式和模型。
    """

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = str(Path.home() / ".bandi" / "data" / "ui_state.db")
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, closing(connect(self._path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ui_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

    def _get(self, key: str, default: str = "") -> str:
        with self._lock, closing(connect(self._path)) as conn:
            row = conn.execute("SELECT value FROM ui_state WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default

    def _set(self, key: str, value: str) -> None:
        with self._lock, closing(connect(self._path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ui_state (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

    @property
    def mode(self) -> str:
        return self._get("mode", "execute")

    @mode.setter
    def mode(self, value: str) -> None:
        self._set("mode", value)

    @property
    def model(self) -> str:
        return self._get("model", "")

    @model.setter
    def model(self, value: str) -> None:
        self._set("model", value)
