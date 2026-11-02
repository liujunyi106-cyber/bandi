# 功能：契约——团队模板一键实例化（build_org_from_template）。
# 钉行为不钉实现：命名空间前缀角色注册（id/引用/can_spawn 全前缀化）、team_id 归属
# 员工 1:1（llm_config 带入 + metadata 标记）、逆拓扑两遍注册、幂等重入（跳过不覆盖 +
# reports_to 收敛重放）、席位注册（recruiter 注入）、profiles 建档 + 技能绑定 best-effort
# 破坏者：team_id 含冒号显式失败；可选注入缺省不崩。
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bandi.company.agent_profile import AgentProfileManager
from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.org_persistence import load_org, save_org
from bandi.company.seat_recruiter import Recruiter, RecruitStatus
from bandi.company.team_template import TeamRoleSpec, TeamTemplate, TeamTemplateError
from bandi.company.team_template_store import build_org_from_template


def _template(team_id: str = "", template_id: str = "ship-team") -> TeamTemplate:
    """功能：基准模板——经理/执行两角色 + 上下级 + per-role 模型配置。"""
    return TeamTemplate(
        template_id=template_id,
        name="发货团队",
        team_id=team_id,
        roles=[
            TeamRoleSpec(
                id="manager",
                name="经理",
                responsibility="管理发货流程",
                can_spawn=["worker"],
                is_final_decider=True,
                tool_tags=["management"],
                llm_config={"default_model": "m-1"},
            ),
            TeamRoleSpec(id="worker", name="执行", reports_to="manager"),
        ],
    )


def test_i1_instantiate_prefix_roles() -> None:
    org = OrgEngine()
    report = build_org_from_template(_template(), org)
    assert report.team_id == "ship-team"
    assert report.role_ids == ["ship-team_manager", "ship-team_worker"]
    assert report.created_role_ids == report.role_ids

    manager = org.get_role("ship-team_manager")
    assert manager is not None
    assert manager.team_id == "ship-team"
    assert manager.is_final_decider is True
    assert manager.tool_tags == ["management"]
    assert manager.can_spawn == ["ship-team_worker"]

    worker = org.get_role("ship-team_worker")
    assert worker is not None
    assert worker.reports_to == "ship-team_manager"
    assert org.get_role("ceo") is not None, "内置组织角色不受实例化影响"


def test_i2_employees_one_to_one() -> None:
    org = OrgEngine()
    report = build_org_from_template(_template(), org)
    assert report.employee_ids == ["ship-team_manager", "ship-team_worker"]

    emp = org.get_employee("ship-team_manager")
    assert emp is not None
    assert emp.role_id == "ship-team_manager"
    assert emp.name == "经理"
    assert emp.llm_config == {"default_model": "m-1"}
    assert emp.metadata["team_id"] == "ship-team"
    assert emp.metadata["source"] == "team_template"

    plain = org.get_employee("ship-team_worker")
    assert plain is not None
    assert plain.llm_config is None


def test_i3_reverse_topology_registers() -> None:
    tpl = TeamTemplate(
        template_id="rev-team",
        name="逆向团队",
        roles=[
            TeamRoleSpec(id="child", name="子", reports_to="parent"),
            TeamRoleSpec(id="parent", name="父"),
        ],
    )
    org = OrgEngine()
    build_org_from_template(tpl, org)
    child = org.get_role("rev-team_child")
    assert child is not None
    assert child.reports_to == "rev-team_parent"


def test_i4_idempotent_rerun_converges() -> None:
    org = OrgEngine()
    build_org_from_template(_template(), org)
    org.get_role("ship-team_worker").name = "用户改名"

    report2 = build_org_from_template(_template(), org)
    assert report2.created_role_ids == []
    assert set(report2.skipped_role_ids) == {"ship-team_manager", "ship-team_worker"}
    assert report2.created_employee_ids == []
    assert org.get_role("ship-team_worker").name == "用户改名", "跳过不覆盖既有角色"

    org.set_reports_to("ship-team_worker", "ceo")
    build_org_from_template(_template(), org)
    assert org.get_role("ship-team_worker").reports_to == "ship-team_manager", (
        "reports_to 必须收敛回模板声明（防中断残状态）"
    )


def test_i5_recruiter_seats() -> None:
    org = OrgEngine()
    recruiter = Recruiter()
    report = build_org_from_template(_template(), org, recruiter=recruiter)
    assert set(report.seat_role_ids) == {"ship-team_manager", "ship-team_worker"}
    seat = recruiter.get_seat("ship-team_manager")
    assert seat is not None
    assert seat.status == RecruitStatus.RECRUITED

    report2 = build_org_from_template(_template(), org, recruiter=recruiter)
    assert report2.seat_role_ids == [], "重入不重复注册席位"


def test_i6_profiles_and_skill_binding_best_effort(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    (skills_dir / "发货技能").mkdir(parents=True)
    (skills_dir / "发货技能" / "SKILL.md").write_text(
        "---\nname: 发货技能\n---\n\n## Instructions\n发货。\n", encoding="utf-8"
    )
    profile_manager = AgentProfileManager(
        profiles_dir=str(tmp_path / "profiles"), skills_dir=str(skills_dir)
    )
    tpl = TeamTemplate(
        template_id="ship-team",
        name="发货",
        roles=[
            TeamRoleSpec(
                id="manager",
                name="经理",
                skill_refs=["发货技能", "ghost-skill"],
            )
        ],
    )
    org = OrgEngine()
    report = build_org_from_template(tpl, org, profile_manager=profile_manager)

    assert report.profile_ids == ["ship-team_manager"]
    profile = profile_manager.get_profile("ship-team_manager")
    assert profile is not None
    assert [r.resource_id for r in profile.resources] == ["发货技能"]
    assert any("ghost-skill" in w for w in report.warnings), (
        f"缺失技能必须进 warnings（best-effort 不中断），实际 {report.warnings}"
    )

    report2 = build_org_from_template(tpl, org, profile_manager=profile_manager)
    assert report2.profile_ids == [], "已有档案不重复建档"


def test_i7_team_id_unsafe_rejected() -> None:
    org = OrgEngine()
    for bad in ("a:b", "a b", "../evil"):
        with pytest.raises(TeamTemplateError):
            build_org_from_template(_template(team_id=bad), org)

    chinese = build_org_from_template(_template(team_id="选品团队"), org)
    assert chinese.team_id == "选品团队"
    assert org.get_role("选品团队_manager") is not None


def test_i8_optional_injections_skipped() -> None:
    org = OrgEngine()
    report = build_org_from_template(_template(), org)
    assert report.seat_role_ids == []
    assert report.profile_ids == []
    assert report.role_ids


def test_i9_org_json_restart_roundtrip(tmp_path: Path) -> None:
    org = OrgEngine()
    recruiter = Recruiter()
    build_org_from_template(_template(), org, recruiter=recruiter)
    org_path = tmp_path / "org.json"
    save_org(org, recruiter, org_path)

    rebuilt = OrgEngine()
    rebuilt_recruiter = Recruiter()
    load_org(rebuilt, rebuilt_recruiter, org_path)

    role = rebuilt.get_role("ship-team_manager")
    assert role is not None
    assert role.team_id == "ship-team"
    assert role.can_spawn == ["ship-team_worker"]
    assert rebuilt.get_role("ship-team_worker").reports_to == "ship-team_manager"

    emp = rebuilt.get_employee("ship-team_manager")
    assert emp is not None
    assert emp.llm_config == {"default_model": "m-1"}, "重启后 per-employee 模型配置不得丢失"

    seat = rebuilt_recruiter.get_seat("ship-team_worker")
    assert seat is not None

    payload = json.loads(org_path.read_text(encoding="utf-8"))
    persisted_seats = set(payload.get("seats", {}))
    assert {"ship-team_manager", "ship-team_worker"} <= persisted_seats, (
        f"席位必须落盘（否则重启后团队卡不可 claim），实际 {persisted_seats}"
    )


def test_i10_conflicting_existing_skips_with_warning() -> None:
    org = OrgEngine()
    org.register_role("ship-team_manager", "旧经理", team_id="other-team")
    org.register_employee(OrgEmployee(employee_id="ship-team_worker", name="旧执行", role_id="ceo"))
    report = build_org_from_template(_template(), org)

    assert "ship-team_manager" in report.skipped_role_ids
    assert any("ship-team_manager" in w for w in report.warnings), (
        f"角色归属冲突必须进 warnings，实际 {report.warnings}"
    )
    assert org.get_role("ship-team_manager").name == "旧经理", "跳过不覆盖既有角色"

    assert "ship-team_worker" in report.skipped_employee_ids
    assert any("ship-team_worker" in w for w in report.warnings), (
        f"员工角色冲突必须进 warnings，实际 {report.warnings}"
    )


def test_i11_worker_prefixed_team_id_persists(tmp_path: Path) -> None:
    org = OrgEngine()
    recruiter = Recruiter()
    report = build_org_from_template(_template(team_id="worker"), org, recruiter=recruiter)
    assert report.created_role_ids

    org_path = tmp_path / "org.json"
    save_org(org, recruiter, org_path)

    rebuilt = OrgEngine()
    rebuilt_recruiter = Recruiter()
    load_org(rebuilt, rebuilt_recruiter, org_path)
    assert rebuilt.get_role("worker_manager") is not None, (
        "worker_ 前缀团队角色不得被误当 init 临时产物丢弃"
    )
    assert rebuilt.get_role("worker_worker") is not None
    assert rebuilt.get_employee("worker_manager") is not None
    assert rebuilt_recruiter.get_seat("worker_worker") is not None
