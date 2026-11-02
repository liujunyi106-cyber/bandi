"""功能：迁移旧团队黑板（默认 dry-run；单团队移动，多团队删除）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bandi.company.comms import COMMS_ROOT_NAME, migrate_project_team_memory  # noqa: E402
from bandi.company.company_operations import CompanyOperationsStore  # noqa: E402
from bandi.company.projects import ProjectStore  # noqa: E402


def _workspace() -> Path:
    return Path.home() / ".bandi" / "company"


def _legacy_files(project_id: str) -> list[str]:
    base = _workspace() / COMMS_ROOT_NAME / project_id
    if not base.is_dir():
        return []
    found: list[str] = []
    for path in base.glob("*/_shared/team_memory/TEAM_MEMORY.md"):
        found.append(str(path))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移旧团队黑板")
    parser.add_argument("--apply", action="store_true", help="执行迁移；缺省仅 dry-run")
    args = parser.parse_args()
    projects = ProjectStore().list_projects()
    workspace = str(_workspace())
    result: dict[str, Any] = {"dry_run": not args.apply, "projects": []}
    operations = CompanyOperationsStore()
    for project in projects:
        legacy = _legacy_files(project.project_id)
        row = {
            "project_id": project.project_id,
            "team_ids": list(project.team_ids),
            "legacy_files": legacy,
        }
        if args.apply and legacy:
            op_id = operations.begin("migrate", f"team_memory:{project.project_id}", "cli")
            try:
                row["results"] = migrate_project_team_memory(
                    workspace, project.project_id, list(project.team_ids)
                )
                operations.commit(op_id)
            except Exception as exc:
                operations.fail(op_id, type(exc).__name__)
                raise
            row["op_id"] = op_id
        result["projects"].append(row)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
