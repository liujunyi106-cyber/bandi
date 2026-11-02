"""功能：把历史 status=retired 员工迁移为硬删除 + 墓碑，默认 dry-run。"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandi.api.app import (  # noqa: E402
    _detach_role_references,
    _employee_tombstones,
    _get_project_store,
    _org_api_dispatcher,
)
from bandi.company.agent_profile import AgentProfileManager  # noqa: E402
from bandi.company.company_operations import CompanyOperationsStore  # noqa: E402
from bandi.company.org_persistence import save_org  # noqa: E402
from bandi.company.team_registry import TeamRegistry  # noqa: E402


def _reconcile_teams(team_ids: set[str], org_engine: Any) -> list[str]:
    dissolved: list[str] = []
    store = _get_project_store()
    for team_id in sorted(team_ids):
        if any(
            str(getattr(role, "team_id", "") or "") == team_id for role in org_engine.list_roles()
        ):
            continue
        TeamRegistry().set_status(team_id, "dissolved")
        dissolved.append(team_id)
        for project in store.list_projects():
            if team_id not in project.team_ids:
                continue
            markers = [*project.dissolved_team_ids]
            if team_id not in markers:
                markers.append(team_id)
            store.save_project(
                project.model_copy(
                    update={
                        "team_ids": [item for item in project.team_ids if item != team_id],
                        "dissolved_team_ids": markers,
                    }
                )
            )
    return dissolved


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移历史 retired 员工")
    parser.add_argument("--apply", action="store_true", help="执行迁移；缺省仅 dry-run")
    args = parser.parse_args()

    dispatcher = _org_api_dispatcher()
    org = dispatcher.org_engine
    employees = list(org.list_employees(include_inactive=True))
    retired = [item for item in employees if str(getattr(item, "status", "")) == "retired"]
    role_counts = Counter(
        str(item.role_id or "").strip() for item in employees if str(item.role_id or "").strip()
    )
    duplicates = sorted(role_id for role_id, count in role_counts.items() if count > 1)
    result: dict[str, Any] = {
        "dry_run": not args.apply,
        "retired": [item.employee_id for item in retired],
        "duplicates": duplicates,
        "purged": [],
        "warnings": [],
        "dissolved_teams": [],
    }
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if duplicates:
        result["error"] = "DUPLICATE_ROLE_ID"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2

    profiles = AgentProfileManager()
    tombstones = _employee_tombstones()
    operations = CompanyOperationsStore()
    op_id = operations.begin("migrate", "retired", "cli")
    result["op_id"] = op_id
    touched_teams: set[str] = set()
    try:
        for employee in retired:
            role_id = str(employee.role_id or "").strip()
            role = org.get_role(role_id) if role_id else None
            other = [
                item
                for item in org.list_employees(include_inactive=True)
                if item.employee_id != employee.employee_id
                and str(item.role_id or "").strip() == role_id
            ]
            profiles.delete_profile(employee.employee_id)
            if role is not None and not other:
                touched_teams.add(str(getattr(role, "team_id", "") or ""))
                _detach_role_references(org, role_id)
                org.remove_role(role_id)
            elif role is not None:
                result["warnings"].append(f"角色仍被其他员工引用: {role_id}")
            org.remove_employee(employee.employee_id)
            tombstones.add(
                employee_id=employee.employee_id,
                role_id=role_id,
                name=employee.name,
                reason="retired_migration",
            )
            result["purged"].append(
                {"employee_id": employee.employee_id, "role_id": role_id, "name": employee.name}
            )
        save_org(org, dispatcher.recruiter)
        result["dissolved_teams"] = _reconcile_teams({item for item in touched_teams if item}, org)
        operations.commit(op_id)
    except Exception as exc:  # noqa: BLE001
        operations.fail(op_id, type(exc).__name__)
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
