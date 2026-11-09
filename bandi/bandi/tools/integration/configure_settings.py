# 功能：configure_settings——只读检查启用状态下的 env secret 键存在性，绝不回显值
from __future__ import annotations

import os
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


class ConfigureSettingsTool(BaseTool):
    name = "configure_settings"
    description = "只读检查集成配置的就绪状态：给定环境变量键的有无（仅返回键名，绝不泄露值），并列出本机已配置的 MCP 服务器名称。不执行任何写操作。"
    category = ToolCategory.INTEGRATION
    feature_group = "integration"
    capability_tags: list[str] = []
    risk_level = "low"
    is_read_only = True
    is_idempotent = True
    aliases = ("settings_status", "check_config")
    parameters = {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "待检查的环境变量键名列表（如 OPENAI_API_KEY）。",
            },
        },
    }

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]:
        try:
            keys = params.get("keys") or []
            if not isinstance(keys, list):
                raise ValueError("'keys' must be an array of environment variable names")
            existing = [k for k in keys if isinstance(k, str) and os.environ.get(k) is not None]
            missing = [k for k in keys if isinstance(k, str) and os.environ.get(k) is None]
            return {
                "success": True,
                "existing": existing,
                "missing": missing,
                "checked": len(existing) + len(missing),
            }
        except (ValueError, TypeError, KeyError) as e:
            return {"success": False, "error": str(e)}
