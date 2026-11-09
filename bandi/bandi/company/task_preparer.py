# 功能：任务预处理——依赖/席位检查 + 策略注入 + rollback

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.phase import Phase, is_terminal
from bandi.company.recruiter import Recruiter, RecruitStatus
from bandi.company.secretary import SecretaryService
from bandi.company.work_item import WorkItem, WorkItemStore, transition_work_item


@dataclass
class TaskPreparationResult:
    # 功能：任务前置准备结果
    ready: bool
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskPreparer:
    # 功能：任务执行前置钩子——检查前置条件/资源锁定/环境准备/失败回滚
    def __init__(self, secretary: SecretaryService | None = None) -> None:
        self._secretary = secretary

    async def prepare(
        self,
        item: WorkItem,
        store: WorkItemStore,
        recruiter: Recruiter,
        runtime: CompanyRuntime,
    ) -> TaskPreparationResult:
        result = self.check_preconditions(item, store)
        if not result.ready:
            return result
        result = self.check_resource_lock(item, recruiter)
        if not result.ready:
            return result
        self._prepare_environment(item, runtime)
        return TaskPreparationResult(ready=True, metadata={"prepared": item.id})

    def check_preconditions(self, item: WorkItem, store: WorkItemStore) -> TaskPreparationResult:
        deps = store.get_many(list(item.children_ids))
        if deps and not all(is_terminal(d.phase) for d in deps):
            return TaskPreparationResult(False, "dependencies not complete")
        return TaskPreparationResult(True)

    def check_resource_lock(self, item: WorkItem, recruiter: Recruiter) -> TaskPreparationResult:
        role_id = item.assignee_role
        if not role_id:
            return TaskPreparationResult(False, "work item has no assignee role")
        seat = recruiter.get_seat(role_id)
        if seat is None:
            return TaskPreparationResult(False, f"no seat registered for role {role_id}")
        if seat.status == RecruitStatus.AVAILABLE:
            return TaskPreparationResult(False, f"seat {role_id} is not claimed")
        return TaskPreparationResult(True)

    def _prepare_environment(self, item: WorkItem, runtime: CompanyRuntime) -> None:
        if runtime is None:
            return
        merged: list[dict[str, Any]] = []
        if self._secretary is not None:
            merged = [
                record.to_dict()
                for record in self._secretary.load_merged(
                    project_id=runtime.project_id,
                    session_id=runtime.session_scope_id,
                )
            ]
        runtime.set_metadata("secretary_policy", merged)

    def rollback(self, item: WorkItem, origin_phase: Phase, store: WorkItemStore) -> None:
        transition_work_item(item, origin_phase, store, reason="task preparation failed")
