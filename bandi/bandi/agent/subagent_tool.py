# 功能：子 Agent 调用工具（主 Agent 委托子 Agent 执行任务）
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bandi.agent.subagent import _SUBAGENT_DEPTH, SubagentConfig, SubagentManager
from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

logger = logging.getLogger(__name__)

# 约束可靠）；超限保留前 N 个，丢弃数经结果 payload warning 公告（非异常）。
# 上限设 10 会被任务数上限 8 卡住，放大动作永不生效。
MAX_SUBAGENT_TASKS = 10


class SubAgentTool(BaseTool):
    name: str = "subagent_spawn"
    description: str = (
        "Spawn one or more sub-agents to work on tasks independently. "
        "Each sub-agent runs its own agent loop with a separate LLM session. "
        "Use for complex multi-step work that benefits from an isolated context; "
        "this call waits for all workers to finish and returns their aggregated results. "
        "Pass multiple tasks to parallelize workers; max_concurrent limits parallelism."
    )
    category: ToolCategory = ToolCategory.UTIL

    def __init__(
        self,
        provider: Any | None = None,
        tool_registry: Any | None = None,
        default_max_concurrency: int = 10,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._tool_registry = tool_registry
        # 未显式传 max_concurrent 时的默认并行上限（factory 注入 config 值；
        # 默认 10 `maxParallelSubCalls=10`）。显式 0 = 不限
        # 但必须告警——静默裸奔会让模型一次给 N 个 tasks 就 N 路全并发。
        raw_cap = int(default_max_concurrency)
        if raw_cap <= 0:
            logger.warning(
                "子代理并发未设上限（default_max_concurrency=%s）—— fan-out 将不受限制",
                default_max_concurrency,
            )
        self._default_max_concurrency = max(0, raw_cap)

    def bind_registry(self, registry: Any) -> None:
        # 功能：后绑定主 registry（factory 阶段 registry 尚未成型，装配点补绑）
        self._tool_registry = registry

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The task description for the sub-agent to work on (single-task mode; ignored when tasks is provided)",
            },
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Worker task list for parallel multi-agent execution (aggregates per-worker results)",
            },
            "tools_allowlist": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Tool names the sub-agent(s) may use (default: inherit all main agent tools)"
                ),
            },
            "max_turns": {
                "type": "integer",
                "description": "Maximum thinking turns for each sub-agent",
            },
            "timeout_sec": {
                "type": "integer",
                "description": "Timeout in seconds for each sub-agent execution",
            },
            "max_tool_calls": {
                "type": "integer",
                "description": "Maximum tool calls per sub-agent (0 = unlimited)",
            },
            "max_depth": {
                "type": "integer",
                "description": "Maximum sub-agent recursion depth (nested spawns)",
            },
            "max_concurrent": {
                "type": "integer",
                "description": (
                    "Max workers running in parallel when tasks provided "
                    "(default: platform limit; pass 0 for unlimited)"
                ),
            },
        },
        "anyOf": [{"required": ["task"]}, {"required": ["tasks"]}],
    }
    risk_level: str = "medium"
    is_concurrency_safe: bool = False
    cancellable: bool = True
    needs_context: bool = True

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        tasks = params.get("tasks") or ([] if params.get("tasks") is not None else None)
        if tasks is None:
            tasks = [params.get("task", "")]
        if isinstance(tasks, str):
            tasks = [tasks]
        tasks = [t for t in tasks if t]
        warning = ""
        if len(tasks) > MAX_SUBAGENT_TASKS:
            dropped = len(tasks) - MAX_SUBAGENT_TASKS
            tasks = tasks[:MAX_SUBAGENT_TASKS]
            warning = f"{dropped} excess tasks dropped (limit {MAX_SUBAGENT_TASKS})"
        config = SubagentConfig(
            tools_allowlist=params.get("tools_allowlist"),
            max_turns=params.get("max_turns", 10),
            timeout_sec=params.get("timeout_sec", 300),
            max_tool_calls=params.get("max_tool_calls", 0),
            max_depth=params.get("max_depth", 3),
        )
        # 使嵌套子 Agent 深度真实递增，深度上限可触发。
        depth = _SUBAGENT_DEPTH.get() + 1
        # 装配对齐（父→子）：沙箱基准 project_root + 权限管道继承自父 executor 注入的
        # context（base.py:77）——子代理写相对路径落父项目根而非全局 workspace
        # 权限约束不打折（无父上下文回落默认管道）
        context_extra = getattr(context, "extra", None) or {}
        project_root = context_extra.get("project_root")
        permissions_pipeline = getattr(context, "permissions_pipeline", None)
        # 自建无订阅总线——成本/trace 断裂；parent_trace_id 同链透传）
        event_bus = getattr(context, "event_bus", None)
        parent_trace_id = str((context_extra.get("trace_id")) or "").strip()
        parent_tool_call_id = str(context_extra.get("tool_call_id") or "").strip()
        # 父会话身份——子代理事件标签的 parent_session_id 来源（嵌套过程归属）
        from types import SimpleNamespace

        parent_context = SimpleNamespace(
            session_id=str(context_extra.get("session_id") or ""),
            bus=event_bus,
            meta={},
        )
        manager = SubagentManager(
            parent_context=parent_context,
            provider=self._provider,
            permissions_pipeline=permissions_pipeline,
            project_root=project_root,
            event_bus=event_bus,
            # .1：角色门控/沙箱继承——父 executor 注入的名单原样下发子代理
            role_tool_tags=context_extra.get("role_tool_tags"),
            role_fs_scopes=context_extra.get("role_fs_scopes"),
            # 装配 HookBefore/After，否则子代理绕过用户配置的 PreToolUse deny）
            hook_runner=context_extra.get("hook_runner"),
            user_id=getattr(context, "user_id", "") or "",
            policy_engine=context_extra.get("policy_engine"),
            parent_agent_id=str(context_extra.get("agent_id") or ""),
        )
        try:
            max_concurrent = params.get("max_concurrent")
            if max_concurrent is None:
                max_concurrent = self._default_max_concurrency
            results = await manager.spawn_many(
                tasks,
                config,
                tool_registry=self._tool_registry,
                parent_trace_id=parent_trace_id or None,
                depth=depth,
                max_concurrent=max_concurrent,
                **({"parent_tool_call_id": parent_tool_call_id} if parent_tool_call_id else {}),
            )
            if len(tasks) > 1:
                payload: dict[str, Any] = {
                    "success": any(r.success for r in results),
                    "workers": [
                        {
                            "task": tasks[i],
                            "task_id": r.task_id,
                            "success": r.success,
                            "summary": r.summary,
                            "message": r.message,
                            "total_tokens": r.total_tokens,
                            "session_id": r.session_id,
                            # .12：结构化结论面随 worker 结果回传
                            "handoff": r.handoff.to_dict() if r.handoff else None,
                        }
                        for i, r in enumerate(results)
                    ],
                    "total_tokens": sum(r.total_tokens for r in results),
                }
                if warning:
                    payload["warning"] = warning
                return payload
            result = results[0] if tasks else None
            if result is None:
                return {"success": False, "summary": "No task provided"}
            return {
                "success": result.success,
                "summary": result.summary,
                "message": result.message,
                "total_tokens": result.total_tokens,
                "session_id": result.session_id,
                "task_id": result.task_id,
                "handoff": result.handoff.to_dict() if result.handoff else None,
            }
        except asyncio.CancelledError:
            manager.cancel_all()
            raise
        finally:
            # 不关会泄漏 sqlite 连接（每次 subagent 调用 +1 连接）
            manager.close()
