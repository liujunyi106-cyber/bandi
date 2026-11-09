# 功能：WorkflowSaveTool——保存/更新工作流 DAG（校验通过即入库，INSERT OR REPLACE 原位更新）
from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolPermissionContext
from bandi.tools.workflow._schema_tool import SchemaWorkflowTool

WORKFLOW_SAVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "工作流名称"},
        "flow_data": {
            "type": "object",
            "description": "DAG 文档（nodes/edges/start_node/end_node）",
        },
        "description": {"type": "string", "description": "工作流描述"},
        "flow_id": {"type": "string", "description": "已有 flow_id 时原地更新；缺省生成新 id"},
        "publish": {"type": "boolean", "description": "是否立即发布（默认 true）"},
    },
    "required": ["name", "flow_data"],
}


class WorkflowSaveTool(SchemaWorkflowTool):
    name: str = "workflow_save"
    description: str = "保存或更新工作流 DAG（通过引擎校验后入库），返回 flow_id 与摘要"
    parameters: dict[str, Any] = WORKFLOW_SAVE_SCHEMA
    is_read_only: bool = False

    async def _execute(
        self, context: ToolPermissionContext | None, **kwargs: Any
    ) -> dict[str, Any]:
        return self._service.save_flow(
            name=str(kwargs["name"]),
            flow_data=kwargs["flow_data"],
            description=str(kwargs.get("description") or ""),
            flow_id=kwargs.get("flow_id"),
            publish=bool(kwargs.get("publish", True)),
        )
