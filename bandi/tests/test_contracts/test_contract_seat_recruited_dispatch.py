# 功能：契约——席位状态机：RECRUITED（模板实例化/重启恢复）必须可被认领执行。
# 破坏者视角：is_claimed 视 RECRUITED 为已认领而 dispatch 仅认 AVAILABLE 时
# 模板实例化/重启恢复后的团队永久无法被调度（静默空转）。
from __future__ import annotations

from bandi.company.org_engine import OrgEngine
from bandi.company.seat_recruiter import Recruiter, RecruitStatus
from bandi.company.team_template import TeamRoleSpec, TeamTemplate
from bandi.company.team_template_store import build_org_from_template


def _recruited(role: str = "worker") -> Recruiter:
    r = Recruiter()
    r.register_seat(role)
    r.recruit(role)
    return r


def test_contract_recruited_seat_dispatch_succeeds() -> None:
    """契约（RECRUITED 可认领）：已招募席位 dispatch 必须成功并转 BUSY——
    模板实例化（build_org_from_template 对每角色 recruit）与重启恢复（org.json
    恢复 RECRUITED）都经此状态，不可认领则团队永久无法被调度执行。"""
    r = _recruited(role="worker")
    assert r.dispatch("worker") is True
    seat = r.get_seat("worker")
    assert seat is not None and seat.status == RecruitStatus.BUSY


def test_contract_busy_seat_rejects_second_dispatch() -> None:
    """契约（互斥不变式）：BUSY 席位再次 dispatch 必须失败（防双执行）；
    释放回 AVAILABLE 后可再次认领。"""
    r = _recruited()
    assert r.dispatch("worker") is True
    assert r.dispatch("worker") is False, "BUSY 必须拒绝重复认领"
    assert r.release("worker") is True
    assert r.dispatch("worker") is True


def test_contract_dispatch_unknown_role_fails() -> None:
    """契约（边界）：未知角色 dispatch 失败。"""
    assert Recruiter().dispatch("ghost") is False


def test_contract_template_instantiate_then_dispatch_e2e() -> None:
    """契约（DoD-12 生产链）：团队模板实例化（席位落 RECRUITED）后，该团队角色
    必须可被认领——「实例化即可工作」的最小数据面证据（两组件端到端串联）。"""
    template = TeamTemplate(
        template_id="aship",
        name="发货团队",
        team_id="aship",
        roles=[TeamRoleSpec(id="worker", name="执行")],
    )
    org = OrgEngine()
    recruiter = Recruiter()
    build_org_from_template(template, org, recruiter=recruiter)
    seat = recruiter.get_seat("aship_worker")
    assert seat is not None and seat.status == RecruitStatus.RECRUITED
    assert recruiter.dispatch("aship_worker") is True, "实例化后的席位必须可认领"
