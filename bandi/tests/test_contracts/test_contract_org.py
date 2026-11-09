# 功能：org 持久化行为契约（防 复审问题复发）
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.org_persistence import load_org, save_org
from bandi.company.recruiter import Recruiter


def _write_org(home: Path, payload: dict) -> None:
    p = home / ".bandi" / "company" / "org.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_contract_order_independent_reports_to(tmp_path: Path) -> None:
    """契约 1：reports_to 后定义不得丢角色（防 角色顺序敏感复发）。"""
    _write_org(
        tmp_path,
        {
            "roles": [
                {
                    "id": "designer",
                    "name": "Designer",
                    "reports_to": "manager",
                    "can_spawn": [],
                    "permissions": {},
                },
                {
                    "id": "manager",
                    "name": "Manager",
                    "reports_to": "ceo",
                    "can_spawn": [],
                    "permissions": {},
                },
            ],
            "employees": [],
            "seats": {},
        },
    )
    engine = OrgEngine()
    with patch("pathlib.Path.home", return_value=tmp_path):
        load_org(engine, Recruiter())
    assert engine.get_role("designer") is not None
    assert engine.get_role("manager") is not None


def test_contract_unknown_reports_to_kept(tmp_path: Path) -> None:
    """契约 2：未知上级降级不丢角色（不崩溃不静默丢）。"""
    _write_org(
        tmp_path,
        {
            "roles": [
                {
                    "id": "ghost",
                    "name": "Ghost",
                    "reports_to": "nonexistent",
                    "can_spawn": [],
                    "permissions": {},
                }
            ],
            "employees": [],
            "seats": {},
        },
    )
    engine = OrgEngine()
    with patch("pathlib.Path.home", return_value=tmp_path):
        load_org(engine, Recruiter())
    assert engine.get_role("ghost") is not None


def test_contract_save_load_roundtrip(tmp_path: Path) -> None:
    """契约 3：save→load roundtrip 全量保留（角色/员工/报告链）。"""
    engine = OrgEngine()
    engine.register_role("ceo", "CEO", "top")
    engine.register_role("designer", "Designer", "ui", reports_to="ceo")
    with patch("pathlib.Path.home", return_value=tmp_path):
        save_org(engine, Recruiter())
        engine2 = OrgEngine()
        load_org(engine2, Recruiter())
    assert engine2.get_role("ceo") is not None
    assert engine2.get_role("designer") is not None


def test_contract_changeset_update_preserves_role_identity() -> None:
    """契约 3a：reorg update 是原位更新——无论 payload 形态如何
    角色 id 恒不变、_roles 字典键与 role.id 恒一致（改 id 只能走 replace）。"""
    from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange

    for payload in (
        {"responsibility": "new"},  # 常规字段更新
        {"id": "hijacked", "name": "Dev2"},  # payload 携带异值 id
        {},  # 空 payload
    ):
        engine = OrgEngine()
        engine.register_role("dev", "Dev", "old")
        changeset = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="update", role_id="dev", role=payload)]
        )
        engine.apply_changeset(changeset)
        role = engine.get_role("dev")
        assert role is not None, f"payload={payload!r} 角色丢失"
        assert role.id == "dev", f"payload={payload!r} id 被改写为 {role.id!r}"
        assert engine._roles.get("dev") is role, "字典键与 role.id 不一致"


def test_contract_load_org_toplevel_not_dict_degrades(tmp_path: Path) -> None:
    """契约 4（批4-1）：顶层非 dict 的损坏 org.json 必须降级不崩——
    CLI/API 启动路径。"""
    for bad in ([], "corrupt", 123):
        _write_org(tmp_path, bad)  # type: ignore[arg-type]
        engine = OrgEngine()
        with patch("pathlib.Path.home", return_value=tmp_path):
            load_org(engine, Recruiter())
        assert engine.get_role("custom_designer") is None, "损坏顶层不加载任何自定义角色"


def test_contract_load_org_bad_role_element_skipped(tmp_path: Path) -> None:
    """契约 5（批4-1）：roles 含非 dict 元素必须跳过且不崩——合法角色仍加载。"""
    _write_org(
        tmp_path,
        {
            "roles": ["garbage", {"id": "designer", "name": "Designer"}],
            "employees": [],
            "seats": {},
        },
    )
    engine = OrgEngine()
    with patch("pathlib.Path.home", return_value=tmp_path):
        load_org(engine, Recruiter())
    assert engine.get_role("designer") is not None, "合法角色必须仍加载"
    assert engine.get_role("garbage") is None


def test_contract_load_org_bad_employee_element_skipped(tmp_path: Path) -> None:
    """契约 6（批4-1）：employees 含非 dict 元素必须跳过不崩。"""
    _write_org(
        tmp_path,
        {
            "roles": [{"id": "ceo", "name": "CEO"}],
            "employees": ["garbage", {"employee_id": "e1", "name": "Alice", "role_id": "ceo"}],
            "seats": {},
        },
    )
    engine = OrgEngine()
    with patch("pathlib.Path.home", return_value=tmp_path):
        load_org(engine, Recruiter())
    assert engine.get_employee("e1") is not None, "合法员工必须仍加载"


def test_contract_load_org_bad_seats_degrades(tmp_path: Path) -> None:
    """契约 7（批4-1）：seats 非 dict（list/str）必须降级不崩。"""
    for bad in ([], "corrupt"):
        _write_org(
            tmp_path,
            {
                "roles": [{"id": "ceo", "name": "CEO"}],
                "employees": [],
                "seats": bad,
            },
        )
        engine = OrgEngine()
        with patch("pathlib.Path.home", return_value=tmp_path):
            load_org(engine, Recruiter())


def test_contract_load_org_bad_employee_metadata_degrades(tmp_path: Path) -> None:
    """契约 8（批4-1）：employees 的 metadata 非 dict（str）必须降级不崩——
    `dict(str)` 抛 ValueError。"""
    _write_org(
        tmp_path,
        {
            "roles": [{"id": "ceo", "name": "CEO"}],
            "employees": [
                {"employee_id": "e1", "name": "Alice", "role_id": "ceo", "metadata": "oops"}
            ],
            "seats": {},
        },
    )
    engine = OrgEngine()
    with patch("pathlib.Path.home", return_value=tmp_path):
        load_org(engine, Recruiter())
    assert engine.get_employee("e1") is not None, "metadata 损坏时员工仍应加载"


def test_contract_org_persistence_roundtrip_full_fields(tmp_path) -> None:
    """
    手写字段清单漏项 = 重启后角色职责/权限静默清空。"""
    from unittest.mock import patch

    from bandi.company.org_persistence import load_org, save_org

    engine = OrgEngine()
    role = engine.register_role("dev", "Dev", "writes code")
    role.permissions = {"bash": "ask", "write": "deny"}
    with patch("pathlib.Path.home", return_value=tmp_path):
        save_org(engine, Recruiter())
        engine2 = OrgEngine()
        load_org(engine2, Recruiter())
    loaded = engine2.get_role("dev")
    assert loaded is not None
    assert loaded.responsibility == "writes code", f"responsibility 丢失: {loaded.responsibility!r}"
    assert loaded.permissions == {"bash": "ask", "write": "deny"}, (
        f"permissions 丢失: {loaded.permissions!r}"
    )


def test_contract_list_employees_excludes_retired() -> None:
    """
    disabled 一律排除（招募池与选派共用此入口，单点修复全链路生效）。"""
    engine = OrgEngine()
    engine.register_role("dev", "Dev")
    active = OrgEmployee(
        employee_id="e1",
        name="Active",
        role_id="dev",
        description="",
        template_id="",
        category="",
        domains=[],
        status="active",
        metadata={},
    )
    retired = OrgEmployee(
        employee_id="e2",
        name="Retired",
        role_id="dev",
        description="",
        template_id="",
        category="",
        domains=[],
        status="retired",
        metadata={},
    )
    engine._employees["e1"] = active
    engine._employees["e2"] = retired

    ids = [e.employee_id for e in engine.list_employees()]
    assert "e1" in ids and "e2" not in ids, f"退役员工泄漏进列表: {ids}"


def test_contract_company_star_import_has_all_exports() -> None:
    """
    star-import 消费方不得崩溃。"""
    import importlib

    mod = importlib.import_module("bandi.company")
    missing = [name for name in getattr(mod, "__all__", []) if not hasattr(mod, name)]
    assert missing == [], f"__all__ 声明了未导出的符号: {missing}"


def test_contract_retired_employee_survives_save_load_roundtrip(tmp_path) -> None:
    """
    list_employees 白名单只作用于招募/选派面，不得改变持久化全量语义
    （否则 reorg 退役员工重启后彻底丢失）。"""
    from unittest.mock import patch

    from bandi.company.org_persistence import load_org, save_org

    engine = OrgEngine()
    engine.register_role("dev", "Dev")
    retired = OrgEmployee(
        employee_id="e-retired",
        name="Old Hand",
        role_id="dev",
        description="",
        template_id="",
        category="",
        domains=[],
        status="retired",
        metadata={},
    )
    engine._employees["e-retired"] = retired

    with patch("pathlib.Path.home", return_value=tmp_path):
        save_org(engine, Recruiter())
        engine2 = OrgEngine()
        load_org(engine2, Recruiter())

    all_ids = [e.employee_id for e in engine2.list_employees(include_inactive=True)]
    assert "e-retired" in all_ids, "retired 员工在持久化 roundtrip 中丢失"
    # 招募/选派面白名单语义：不出现在默认列表
    active_ids = [e.employee_id for e in engine2.list_employees()]
    assert "e-retired" not in active_ids
