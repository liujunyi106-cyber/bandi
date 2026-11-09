# 功能：WorkflowEmbedEmitTool——把工作流 DAG 校验后发布为聊天卡片 payload
# （digest + 规范化 flow_data + title/summary/flow_id），纯只读不落库。
from __future__ import annotations

import hashlib
import json
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.workflow.engine import NodeExecutionError, WorkflowEngine

WORKFLOW_EMBED_EMIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "卡片标题（短标题，展示在流程图上方）"},
        "summary": {"type": "string", "description": "可选摘要"},
        "flow_data": {
            "type": "object",
            "description": "DAG 文档（nodes/edges/start_node/end_node，与 Flow.data 同形状）",
        },
        "flow_id": {
            "type": "string",
            "description": "可选：已保存 flow_id（workflow_save 产出，供卡片深链/运行）",
        },
    },
    "required": ["title", "flow_data"],
}


class WorkflowEmbedEmitTool(BaseTool):
    name: str = "workflow_embed_emit"
    description: str = (
        "把工作流 DAG（nodes/edges）校验后发布为聊天卡片：返回 digest 与规范化 "
        "flow_data，供前端渲染流程图并运行（与 workflow_save 同 digest 身份）。"
        "纯只读不落库；要运行请先用 workflow_save 拿 flow_id 再携带发布。"
    )
    parameters: dict[str, Any] = WORKFLOW_EMBED_EMIT_SCHEMA
    category: ToolCategory = ToolCategory.WORKFLOW
    feature_group: str = "workflow"
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    async def execute(self, params: dict[str, Any], context: Any = None) -> str | dict[str, Any]:
        flow_data = params.get("flow_data")
        if not isinstance(flow_data, dict):
            return {"ok": False, "error": "flow_data 必须是对象（nodes/edges DAG 文档）"}
        try:
            node_count = WorkflowEngine().validate(flow_data)
        except NodeExecutionError as exc:
            return {"ok": False, "error": f"Invalid workflow graph: {exc}"}
        digest = hashlib.sha256(
            json.dumps(flow_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "success": True,
            "title": str(params.get("title") or ""),
            "summary": str(params.get("summary") or ""),
            "flow_id": str(params.get("flow_id") or ""),
            "digest": digest,
            "node_count": node_count,
            "flow_data": flow_data,
        }
