"""契约族：旧库升级迁移（schema migration）——启动时幂等 ALTER + 旧数据保留。

生产调用路径：
1. WorkItemStore.__init__——启动建表/加列（_ensure_schema 幂等迁移）
2. store.save_org_roles / load_org_roles——迁移后旧行可读回、新列默认值生效
3. 旧版本 org_roles 表（无 team_id 列）→ 新 store 打开 → 列补齐且旧字段不丢

断言「最终产物 / 行为」：旧库文件打开即升级（team_id 列出现）、旧数据逐字段
保留（name/can_spawn/permissions/is_final_decider）、team_id 默认空串不破坏读取。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bandi.company.store import WorkItemStore


def _make_legacy_db(path: Path) -> None:
    """构造旧版本 org_roles 表（迁移前的 schema，无 team_id 列）+ 存量行。"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS org_roles (
            role_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            reports_to TEXT,
            can_spawn TEXT DEFAULT '[]',
            responsibility TEXT DEFAULT '',
            permissions TEXT DEFAULT '{}',
            tool_tags TEXT DEFAULT '[]',
            is_final_decider INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO org_roles (role_id, name, can_spawn, permissions, is_final_decider,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-dev",
            "LegacyDev",
            json.dumps(["fs"]),
            json.dumps({"scope": "repo"}),
            1,
            "T00:00:00+00:00",
            "T00:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO org_roles (role_id, name, can_spawn, is_final_decider,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "legacy-qa",
            "LegacyQA",
            "[]",
            0,
            "T00:00:00+00:00",
            "T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def test_legacy_db_opened_upgrades_with_team_id_column(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)
    WorkItemStore(str(db))  # 打开即迁移

    cols = [row[1] for row in store_conn(db).execute("PRAGMA table_info(org_roles)").fetchall()]
    assert "team_id" in cols, "旧库打开后必须补齐 team_id 列（启动时幂等 ALTER）"


def test_legacy_rows_preserved_past_migration(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)
    store = WorkItemStore(str(db))
    roles = store.load_org_roles()

    dev = next(r for r in roles if r["role_id"] == "legacy-dev")
    assert dev["name"] == "LegacyDev"
    assert dev["can_spawn"] == ["fs"] and dev["permissions"] == {"scope": "repo"}
    assert dev["is_final_decider"] is True
    assert dev["team_id"] == "", "旧行无 team_id → 迁移后默认空串（不破坏读取）"

    qa = next(r for r in roles if r["role_id"] == "legacy-qa")
    assert qa["name"] == "LegacyQA" and qa["team_id"] == ""


def test_rerun_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _make_legacy_db(db)
    WorkItemStore(str(db))  # 首次打开触发迁移
    store2 = WorkItemStore(str(db))  # 二次打开：不得报 duplicate column
    roles = store2.load_org_roles()
    assert len([r for r in roles if r["role_id"].startswith("legacy-")]) == 2


def store_conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn
