# 功能：Workflow 工具适配基类（SchemaWorkflowTool；Bandi 侧 execute(params, context) → _execute(context, **kwargs)）
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

if TYPE_CHECKING:
    from bandi.tools.workflow.service import WorkflowService


class SchemaWorkflowTool(BaseTool, ABC):
    category: ToolCategory = ToolCategory.WORKFLOW
    feature_group: str = "workflow"
    capability_tags: list[str] = ["database"]
    risk_level: str = "low"
    is_concurrency_safe: bool = True
    dedup_scope: str = "none"

    def __init__(self, service: WorkflowService) -> None:
        super().__init__()
        self._service = service

    @abstractmethod
    async def _execute(
        self, context: ToolPermissionContext | None, **kwargs: Any
    ) -> dict[str, Any]: ...

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]:
        if not hasattr(self, "_service") or self._service is None:
            return {"ok": False, "error": "workflow service 未注入"}
        return await self._execute(context=context, **params)
