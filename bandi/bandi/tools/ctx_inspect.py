from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


class CtxInspectTool(BaseTool):
    name: str = "ctx_inspect"
    description: str = (
        "Inspect the current agent runtime context: token usage vs budget, "
        "message count, active tools, compression history and session info. "
        "Use for self-checking context health before long tasks."
    )
    category: ToolCategory = ToolCategory.UTIL
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        # 快照经 executor 每轮注入（states.agent_runtime_snapshot →
        # ToolPermissionContext.extra）；standalone 调用无绑定时不伪造数据
        snapshot = (getattr(context, "extra", None) or {}).get("runtime_snapshot")
        if not snapshot:
            return {
                "available": False,
                "reason": "runtime snapshot not bound (standalone invocation)",
            }
        return {"available": True, **snapshot}
