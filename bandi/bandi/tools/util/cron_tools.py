# 功能：cron_tools — 定时任务登记/查询/删除（写 TaskStore；调度器拾取执行——cron 桥接）
from __future__ import annotations

import json
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from bandi.scheduler.store import ScheduledTask, TaskStore
from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext


class CronJobStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


def default_task_store() -> TaskStore:
    """功能：默认调度库（与 api/app.py 的 TaskStore 同源路径）；惰性求值
    （Path.home patch 生效——测试隔离，同族）。"""
    return TaskStore(str(Path.home() / ".bandi" / "data" / "scheduler.db"))


def _is_valid_cron(schedule: str) -> bool:
    """功能：cron 表达式校验——与调度器 CronExpr 解析器同源（标准 5 字段
    别名 @daily 等支持）。6 段/Quartz 语法（?LW#）本调度器无法执行
    必须在此拒绝（登记成功却永不触发 = 定时炸弹）。"""
    from bandi.scheduler.cron import CronExpr, InvalidCronExpressionError

    try:
        CronExpr(schedule)
        return True
    except InvalidCronExpressionError:
        return False


def _synthesize_prompt(
    name: str, target_type: str, target_id: str | None, payload: dict[str, Any]
) -> str:
    """功能：无显式 prompt 时按 target 元数据合成执行描述——调度触发以 headless
    agent turn 消费 prompt（空 prompt 任务不可执行）。"""
    lines = [f"[定时任务] {name}", f"目标类型：{target_type}"]
    if target_id:
        lines.append(f"目标：{target_id}")
    if payload:
        lines.append(f"参数：{json.dumps(payload, ensure_ascii=False)}")
    return "\n".join(lines)


def _job_payload(task: ScheduledTask) -> dict[str, Any]:
    """功能：TaskStore 记录 → cron 工具 job 形态（旧契约字段面保持）。"""
    return {
        "id": task.task_id,
        "name": task.name,
        "schedule": task.cron,
        "target_type": task.target_type,
        "target_id": task.target_id or None,
        "description": task.description,
        "payload": task.payload,
        "session_id": (task.payload or {}).get("session_id", ""),
        "project_id": (task.payload or {}).get("project_id", ""),
        "employee_id": (task.payload or {}).get("employee_id", ""),
        "enabled": task.enabled,
        "status": (CronJobStatus.ACTIVE.value if task.enabled else CronJobStatus.DISABLED.value),
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
    }


class _CronBase(BaseTool):
    feature_group = "util"
    is_read_only = False
    is_concurrency_safe = True
    # 功能：会话内创建的任务绑定当前 session/project/employee
    needs_context = True
    capability_tags: list[str] = [ToolCapability.DATABASE.value]

    def __init__(self, store: TaskStore | None = None) -> None:
        self._store = store or default_task_store()


class CronCreateTool(_CronBase):
    """功能：登记新定时任务（校验 schedule 形态 + payload 必须 dict；写入调度库）。"""

    name = "cron_create"
    description = (
        "创建定时任务。用户说每天/每周/每月某个时间自动执行时调用；"
        "schedule 用 5 段 cron，例如每天 12:30 = '30 12 * * *'，"
        "每周一 9:00 = '0 9 * * 1'。任务会绑定当前会话，到点在原会话继续执行。"
    )
    category = ToolCategory.UTIL
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "任务名，例如：拉取 AI 新闻"},
            "schedule": {"type": "string", "description": "5 段 cron，例如 30 12 * * *"},
            "prompt": {
                "type": "string",
                "description": "到点执行的完整指令，例如：拉取过去 24 小时的 AI 新闻并摘要",
            },
            "target_type": {
                "type": "string",
                "enum": ["assistant", "employee", "workflow", "task", "webhook", "script"],
                "description": "会话内默认由当前会话推断；显式指定时优先",
            },
            "target_id": {"type": "string"},
            "description": {"type": "string"},
            "payload": {"type": "object"},
            "enabled": {"type": "boolean", "default": True},
        },
        "required": ["name", "schedule"],
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        name = params.get("name")
        schedule = params.get("schedule")
        extra = context.extra if context and isinstance(context.extra, dict) else {}
        session_id = str(extra.get("session_id") or "").strip()
        project_id = str(extra.get("project_id") or "").strip()
        employee_id = str(extra.get("employee_id") or "").strip()
        # 功能：会话内任务按当前会话绑定执行者；无会话上下文保持旧 target 语义
        if session_id:
            if employee_id:
                target_type = "employee"
                target_id = employee_id
            else:
                target_type = "assistant"
                target_id = ""
        else:
            target_type = str(params.get("target_type") or "task")
            target_id = str(params.get("target_id") or "")
        if not name:
            return {"ok": False, "error": "name is required"}
        if not schedule or not _is_valid_cron(schedule):
            return {"ok": False, "error": f"invalid cron schedule: {schedule}"}
        payload = params.get("payload", {})
        if payload is not None and not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be an object"}
        payload = dict(payload or {})
        if session_id:
            payload["session_id"] = session_id
        if project_id:
            payload["project_id"] = project_id
        if employee_id:
            payload["employee_id"] = employee_id
        prompt = str(params.get("prompt") or "").strip() or _synthesize_prompt(
            str(name), str(target_type), target_id, payload
        )
        enabled = bool(params.get("enabled", True))
        task = ScheduledTask(
            task_id=uuid.uuid4().hex,
            name=str(name),
            cron=str(schedule),
            prompt=prompt,
            enabled=enabled,
            target_type=str(target_type),
            target_id=target_id,
            description=str(params.get("description") or ""),
            payload=payload,
        )
        saved = self._store.save(task)
        return {"ok": True, "job": _job_payload(saved)}


class CronListTool(_CronBase):
    """功能：列出定时任务（enabled/target_type/limit 过滤；从调度库读）。"""

    name = "cron_list"
    description = "列出定时任务：enabled/target_type/limit"
    category = ToolCategory.UTIL
    is_read_only = True
    parameters = {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "target_type": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
        },
        "required": [],
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        enabled = params.get("enabled")
        target_type = params.get("target_type")
        limit = int(params.get("limit", 50) or 50)
        jobs: list[dict[str, Any]] = []
        for task in self._store.list():
            if enabled is not None and task.enabled != bool(enabled):
                continue
            if target_type and task.target_type != target_type:
                continue
            jobs.append(_job_payload(task))
        jobs = jobs[:limit]
        return {"ok": True, "count": len(jobs), "jobs": jobs}


class CronDeleteTool(_CronBase):
    """功能：删除定时任务（幂等：不存在返回 removed=False）。"""

    name = "cron_delete"
    description = "删除定时任务：job_id"
    category = ToolCategory.UTIL
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
        },
        "required": ["job_id"],
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        job_id = params.get("job_id")
        if not job_id:
            return {"ok": False, "error": "job_id is required", "removed": False}
        removed = self._store.get(str(job_id)) is not None
        if removed:
            self._store.delete(str(job_id))
        return {"ok": True, "removed": removed, "job_id": job_id}
