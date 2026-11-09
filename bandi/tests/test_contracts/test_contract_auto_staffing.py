"""功能：自动补员契约——注册角色补齐员工+座位；未注册角色明确返回；未注册 @ 提示清单。"""

from __future__ import annotations

from bandi.api.company_chat import unresolved_mentions
from bandi.company.org_engine import OrgEmployee, OrgEngine
from bandi.company.seat_recruiter import Recruiter
from bandi.company.staffing import ensure_role_staffed


def _fresh() -> tuple[OrgEngine, Recruiter]:
    return OrgEngine(), Recruiter()


class TestEnsureRoleStaffed:
    def test_registered_role_gets_employee_and_seat(self) -> None:
        org, recruiter = _fresh()
        outcome = ensure_role_staffed(org, recruiter, "ceo")
        assert outcome == "ok"
        seat = recruiter.get_seat("ceo")
        assert seat is not None
        employee = org.get_employee("ceo")
        assert employee is not None
        assert employee.role_id == "ceo"
        assert employee.metadata.get("source") == "auto_staffing"
        assert recruiter.dispatch("ceo") is True

    def test_idempotent_second_call(self) -> None:
        org, recruiter = _fresh()
        assert ensure_role_staffed(org, recruiter, "cto") == "ok"
        assert ensure_role_staffed(org, recruiter, "cto") == "ok"
        assert len([s for s in recruiter.list_seats() if s.role_id == "cto"]) == 1

    def test_unregistered_role_returns_not_registered(self) -> None:
        org, recruiter = _fresh()
        assert ensure_role_staffed(org, recruiter, "ghost_role") == "role-not-registered"
        assert recruiter.get_seat("ghost_role") is None

    def test_existing_employee_not_overwritten(self) -> None:
        org, recruiter = _fresh()
        org.register_employee(
            OrgEmployee(employee_id="em", name="工程经理", role_id="em", description="原有的")
        )
        assert ensure_role_staffed(org, recruiter, "em") == "ok"
        employee = org.get_employee("em")
        assert employee is not None and employee.description == "原有的"
        assert recruiter.get_seat("em") is not None


class _FakeOrg:
    def list_roles(self) -> list[object]:
        return [type("Role", (), {"id": r, "name": r})() for r in ("lead", "designer")]


class TestUnresolvedMentions:
    def test_registered_and_at_all_unresolved_lists(self) -> None:
        assert unresolved_mentions("@designer 做一版方案", _FakeOrg()) == []
        assert unresolved_mentions("@all 各位好", _FakeOrg()) == ["all"]
        assert unresolved_mentions("没有提及", _FakeOrg()) == []

    def test_ghost_mentions_listed(self) -> None:
        assert unresolved_mentions("@ghost 做个事", _FakeOrg()) == ["ghost"]
        assert unresolved_mentions("@ghost @designer 一起", _FakeOrg()) == ["ghost"]

    def test_none_org_treats_all_as_unresolved(self) -> None:
        assert unresolved_mentions("@designer 做一版方案", None) == ["designer"]
