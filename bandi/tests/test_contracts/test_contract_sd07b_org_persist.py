# 功能：契约——组织持久化收口（=A：org.json 为生产单源，DB 通道保真性修复）。
# 钉行为不钉实现：
# ① org_employees 表 llm_config 列（旧库 ALTER 迁移 + None/{}/值 三态往返保真）
# ② OrgEngine.persist(store)/restore 往返保留 per-employee llm_config
# ③ DB 通道非生产标注（行为不变，仅保真性）。
from __future__ import annotations

import sqlite3
from pathlib import Path

from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.store import WorkItemStore


def _legacy_db_with_employee(tmp_path: Path) -> Path:
    """功能：构造旧 schema 库——org_employees 无 llm_config 列 + 一条旧员工。"""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE org_employees (
            employee_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role_id TEXT NOT NULL,
            template_id TEXT DEFAULT '',
            description TEXT DEFAULT '',
            category TEXT DEFAULT '',
            domains TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("INSERT INTO org_employees VALUES ")
    conn.commit()
    conn.close()
    return db


def test_p1_legacy_schema_migration_adds_llm_config(tmp_path: Path) -> None:
    db = _legacy_db_with_employee(tmp_path)
    store = WorkItemStore(str(db))
    employees = store.load_org_employees()
    assert employees[0]["employee_id"] == "e1"
    assert employees[0]["llm_config"] is None, "旧行迁移后 llm_config 默认 None"

    store.save_org_employees([{**employees[0], "llm_config": {"default_model": "m-2"}}])
    assert store.load_org_employees()[0]["llm_config"] == {"default_model": "m-2"}


def test_p2_engine_persist_restore_keeps_llm_config(tmp_path: Path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    org = OrgEngine(persist_store=store)
    org.register_role("custom-role", "自定义角色")
    org.register_employee(
        OrgEmployee(
            employee_id="e1",
            name="员工甲",
            role_id="custom-role",
            llm_config={"default_model": "m-3", "base_url": "https://private.gw"},
        )
    )

    rebuilt = OrgEngine(persist_store=store)
    got = rebuilt.get_employee("e1")
    assert got is not None
    assert got.llm_config == {"default_model": "m-3", "base_url": "https://private.gw"}, (
        "DB 通道往返必须保留 per-employee llm_config（EX-3 换大脑断链防线）"
    )
    assert rebuilt.get_role("custom-role") is not None


def test_p3_llm_config_three_states(tmp_path: Path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    store.save_org_employees(
        [
            {"employee_id": "a", "name": "A", "role_id": "r", "llm_config": None},
            {"employee_id": "b", "name": "B", "role_id": "r", "llm_config": {}},
            {"employee_id": "c", "name": "C", "role_id": "r", "llm_config": {"default_model": "m"}},
        ]
    )
    rows = {e["employee_id"]: e["llm_config"] for e in store.load_org_employees()}
    assert rows["a"] is None
    assert rows["b"] == {}
    assert rows["c"] == {"default_model": "m"}


def test_p4_migration_idempotent(tmp_path: Path) -> None:
    db = _legacy_db_with_employee(tmp_path)
    first = WorkItemStore(str(db))
    first.save_org_employees([{"employee_id": "e1", "name": "旧员工", "role_id": "r1"}])
    first.close()

    second = WorkItemStore(str(db))
    rows = second.load_org_employees()
    assert len(rows) == 1
    assert rows[0]["llm_config"] is None
