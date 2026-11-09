# 功能：ChatWorkflowTool——会话内把工具步骤序列整理为结构化工作流草稿（校验 registry 引用，不落库）
from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from bandi.tools.base import BaseTool, ToolCategory

if TYPE_CHECKING:
    from bandi.tools.registry import ToolRegistry

CHAT_WORKFLOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {"type": "integer", "description": "工作流版本，默认 1"},
        "title": {"type": "string", "description": "工作流标题"},
        "summary": {"type": "string", "description": "一句话摘要"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]+$"},
                    "label": {"type": "string"},
                    "hint": {"type": "string"},
                    "action": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["tool"]},
                            "tool_id": {"type": "string"},
                            "arguments": {"type": "object"},
                        },
                        "required": ["kind", "tool_id", "arguments"],
                    },
                },
                "required": ["id", "action"],
            },
        },
    },
    "required": ["title", "steps"],
}


class ChatWorkflowTool(BaseTool):
    name: str = "chat_workflow"
    description: str = (
        "把会话里的工具步骤序列整理为结构化工作流（引用 registry 已注册工具，fail-closed）"
    )
    parameters: dict[str, Any] = CHAT_WORKFLOW_SCHEMA
    category: ToolCategory = ToolCategory.WORKFLOW
    feature_group: str = "workflow"
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__()
        self._registry = registry

    async def execute(self, params: dict[str, Any], context: Any = None) -> str | dict[str, Any]:
        steps = params.get("steps") or []
        seen: set[str] = set()
        for step in steps:
            step_id = str(step.get("id") or "")
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", step_id):
                return {"ok": False, "error": f"步骤 id 非法（仅字母/数字/_/-）: {step_id!r}"}
            if step_id in seen:
                return {"ok": False, "error": f"步骤 id 重复: {step_id}"}
            seen.add(step_id)
        for step in steps:
            tool_id = str((step.get("action") or {}).get("tool_id") or "")
            tool = self._registry.get(tool_id)
            if tool is None:
                return {"ok": False, "error": f"工具未注册或特性组未开启: {tool_id}"}
            if getattr(tool, "is_destructive", False):
                return {
                    "ok": False,
                    "error": f"工具不可用于只读工作流草稿（destructive）: {tool_id}",
                }
        spec = {
            "version": int(params.get("version") or 1),
            "title": str(params.get("title") or ""),
            "summary": str(params.get("summary") or ""),
            "steps": steps,
        }
        digest = hashlib.sha256(
            json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {"success": True, "workflow": spec, "digest": digest}
