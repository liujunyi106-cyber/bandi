# 功能：工作项运行时不变式——5 项诊断检查

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from bandi.company.phase import Phase, is_terminal
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class InvariantViolation:
    item_id: str
    severity: Severity
    category: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "severity": self.severity.value,
            "category": self.category,
            "message": self.message,
        }


@dataclass
class CheckResult:
    violations: list[InvariantViolation] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(v.severity == Severity.ERROR for v in self.violations)

    @property
    def has_warnings(self) -> bool:
        return any(v.severity == Severity.WARNING for v in self.violations)


def check_invariants(store: WorkItemStore) -> CheckResult:
    items = store.list_all()
    result = CheckResult()

    item_map = {i.id: i for i in items}

    _check_orphan_children(items, item_map, result)
    _check_cycle_dependency(items, result)
    _check_terminal_phases(items, result)
    _check_assignee_consistency(items, result)
    _check_running_consistency(items, result)

    return result


def _check_orphan_children(
    items: list[WorkItem], item_map: dict[str, WorkItem], result: CheckResult
) -> None:
    for item in items:
        if item.parent_id is not None and item.parent_id not in item_map:
            result.violations.append(
                InvariantViolation(
                    item_id=item.id,
                    severity=Severity.ERROR,
                    category="orphan",
                    message=f"parent {item.parent_id} not found",
                )
            )


def _check_cycle_dependency(items: list[WorkItem], result: CheckResult) -> None:
    item_map = {i.id: i for i in items}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def dfs(item_id: str, path: list[str]) -> None:
        if item_id in in_stack:
            cycle = path[path.index(item_id) :] + [item_id]
            result.violations.append(
                InvariantViolation(
                    item_id=item_id,
                    severity=Severity.ERROR,
                    category="cycle",
                    message=f"cycle detected: {' -> '.join(cycle)}",
                )
            )
            return
        if item_id in visited:
            return
        if item_id not in item_map:
            return
        visited.add(item_id)
        in_stack.add(item_id)
        path.append(item_id)
        item = item_map[item_id]
        for child_id in item.children_ids:
            dfs(child_id, path)
        path.pop()
        in_stack.discard(item_id)

    for item in items:
        if item.id not in visited:
            dfs(item.id, [])


def _check_terminal_phases(items: list[WorkItem], result: CheckResult) -> None:
    for item in items:
        if is_terminal(item.phase) and item.children_ids:
            result.violations.append(
                InvariantViolation(
                    item_id=item.id,
                    severity=Severity.WARNING,
                    category="terminal_with_children",
                    message=f"terminal phase {item.phase.value} but has {len(item.children_ids)} children",
                )
            )


def _check_assignee_consistency(items: list[WorkItem], result: CheckResult) -> None:
    for item in items:
        if item.phase == Phase.READY_FOR_REWORK and not item.assignee_role:
            result.violations.append(
                InvariantViolation(
                    item_id=item.id,
                    severity=Severity.WARNING,
                    category="assignee_missing",
                    message="READY_FOR_REWORK without assignee_role",
                )
            )


def _check_running_consistency(items: list[WorkItem], result: CheckResult) -> None:
    for item in items:
        if item.phase == Phase.RUNNING:
            last_output = item.runtime_metadata.get("_last_output", "")
            if not last_output:
                result.violations.append(
                    InvariantViolation(
                        item_id=item.id,
                        severity=Severity.WARNING,
                        category="running_no_output",
                        message="RUNNING but _last_output is empty",
                    )
                )
