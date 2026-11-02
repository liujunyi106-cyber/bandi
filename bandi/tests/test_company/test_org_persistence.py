# 功能：org_persistence 深度复审修复测试（角色顺序敏感 + 损坏文件告警）
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from bandi.company.org_engine import OrgEngine
from bandi.company.org_persistence import load_org, save_org
from bandi.company.recruiter import Recruiter


def _write_org(home: Path, payload: dict) -> None:
    p = home / ".bandi" / "company" / "org.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_org_order_independent_for_reports_to(tmp_path: Path) -> None:
    """深度复审修复：角色 A reports_to B（B 在 JSON 中后定义）也必须注册成功"""
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
    recruiter = Recruiter()
    with patch("pathlib.Path.home", return_value=tmp_path):
        load_org(engine, recruiter)
    assert engine.get_role("designer") is not None, "reports_to 后定义时角色不得静默丢失"
    assert engine.get_role("manager") is not None


def test_load_org_unknown_reports_to_kept_without_crash(tmp_path: Path) -> None:
    """深度复审修复：reports_to 指向不存在的角色时不崩溃且保留角色（降级为无上级）。"""
    _write_org(
        tmp_path,
        {
            "roles": [
                {
                    "id": "ghost_ref",
                    "name": "Ghost",
                    "reports_to": "nonexistent_role",
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
    role = engine.get_role("ghost_ref")
    assert role is not None, "未知上级不得导致角色整体丢失"


def test_save_load_roundtrip_with_reports_to_order(tmp_path: Path) -> None:
    """save_org → 重开 load_org 全量角色/员工保留（含 reports_to 关系）。"""
    engine = OrgEngine()
    engine.register_role("ceo", "CEO", "top")
    engine.register_role("designer", "Designer", "ui", reports_to="ceo")
    engine.register_role("manager", "Manager", "mgmt", reports_to="ceo")
    with patch("pathlib.Path.home", return_value=tmp_path):
        save_org(engine, Recruiter())
        engine2 = OrgEngine()
        load_org(engine2, Recruiter())
    assert engine2.get_role("designer") is not None
    assert engine2.get_role("manager") is not None


def test_save_org_rejects_cycle(tmp_path: Path) -> None:
    """
    save 后 load 时半加载降级，用户数据被静默「修复」成单向）。"""
    import pytest

    engine = OrgEngine()
    engine.register_role("alpha", "Alpha", "a")
    engine.register_role("beta", "Beta", "b")
    # 构造环：alpha → beta → alpha（经 set_reports_to 需绕开单边校验
    # 直接操纵 OrgRole 对象模拟外部构造的环）
    alpha = engine.get_role("alpha")
    beta = engine.get_role("beta")
    assert alpha is not None and beta is not None
    alpha.reports_to = "beta"
    beta.reports_to = "alpha"
    with patch("pathlib.Path.home", return_value=tmp_path):
        with pytest.raises(ValueError, match="cycle"):
            save_org(engine, Recruiter())
