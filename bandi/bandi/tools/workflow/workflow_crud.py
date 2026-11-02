# 功能：WorkflowTool 工具族——workflow_list/run/status/cancel/pause/resume
from __future__ import annotations

from typing import Any

from bandi.tools.workflow._schema_tool import SchemaWorkflowTool


class WorkflowListTool(SchemaWorkflowTool):
    name: str = "workflow_list"
    description: str = "列出已保存的工作流（DAG），可按 flow_type/status 过滤。"
    is_read_only: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "flow_type": {
                "type": "string",
                "enum": ["agent", "workflow", "chat", "tool"],
                "description": "按流程类型过滤（默认全部）",
            },
            "status": {
                "type": "string",
                "enum": ["draft", "published", "archived"],
                "description": "按状态过滤（默认全部）",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "最大返回条数（默认 20）",
            },
        },
    }

    async def _execute(self, context, **kwargs) -> dict[str, Any]:
        try:
            flows = self._service.list_flows(
                flow_type=kwargs.get("flow_type"),
                status=kwargs.get("status"),
                limit=kwargs.get("limit") or 20,
            )
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"workflow_list 参数非法: {exc}"}
        return {"flows": flows}


class WorkflowRunTool(SchemaWorkflowTool):
    name: str = "workflow_run"
    description: str = (
        "运行已保存的工作流（DAG），传入 inputs 作为节点输入变量；"
        "返回 execution_id/最终 outputs 与运行状态。"
    )
    is_read_only: bool = False
    is_idempotent: bool = False
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "flow_id": {
                "type": "string",
                "minLength": 1,
                "description": "目标工作流 ID（workflow_list 可查）",
            },
            "inputs": {
                "type": "object",
                "description": "运行输入变量（供 ${inputs.xxx} 模板引用）",
            },
            "user_id": {"type": "string", "description": "触发人（默认空串）"},
        },
        "required": ["flow_id"],
    }

    async def _execute(self, context, **kwargs) -> dict[str, Any]:
        flow_id = str(kwargs.get("flow_id") or "").strip()
        if not flow_id:
            return {"ok": False, "error": "flow_id 必填"}
        inputs = kwargs.get("inputs") or {}
        if not isinstance(inputs, dict):
            return {"ok": False, "error": "inputs 必须是对象（key→value）"}
        return await self._service.start(
            flow_id,
            user_id=str(kwargs.get("user_id") or ""),
            inputs=inputs,
            trigger_type="tool",
        )


class WorkflowStatusTool(SchemaWorkflowTool):
    name: str = "workflow_status"
    description: str = "查询工作流执行记录：状态/输入/最终输出/逐节点执行历史。"
    is_read_only: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "execution_id": {
                "type": "string",
                "minLength": 1,
                "description": "workflow_run 返回的 execution_id",
            }
        },
        "required": ["execution_id"],
    }

    async def _execute(self, context, **kwargs) -> dict[str, Any]:
        execution_id = str(kwargs.get("execution_id") or "").strip()
        if not execution_id:
            return {"ok": False, "error": "execution_id 必填"}
        record = await self._service.get_execution(execution_id)
        if record is None:
            return {"ok": False, "error": f"execution {execution_id} not found"}
        return record


class WorkflowCancelTool(SchemaWorkflowTool):
    name: str = "workflow_cancel"
    description: str = "取消正在运行/排队的工作流执行（仅 running/pending 可取消）。"
    is_read_only: bool = False
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "execution_id": {
                "type": "string",
                "minLength": 1,
                "description": "待取消的 execution_id",
            }
        },
        "required": ["execution_id"],
    }

    async def _execute(self, context, **kwargs) -> dict[str, Any]:
        execution_id = str(kwargs.get("execution_id") or "").strip()
        if not execution_id:
            return {"ok": False, "error": "execution_id 必填"}
        if await self._service.cancel_execution(execution_id):
            return {"execution_id": execution_id, "status": "cancelled"}
        record = await self._service.get_execution(execution_id)
        state = record["status"] if record is not None else "unknown"
        return {"ok": False, "error": f"execution {execution_id} 状态为 {state}，不可取消"}


class WorkflowPauseTool(SchemaWorkflowTool):
    name: str = "workflow_pause"
    description: str = (
        "暂停正在运行的工作流执行；引擎在下个节点边界落 checkpoint，之后可 workflow_resume 续跑。"
    )
    is_read_only: bool = False
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "execution_id": {
                "type": "string",
                "minLength": 1,
                "description": "待暂停的 execution_id",
            }
        },
        "required": ["execution_id"],
    }

    async def _execute(self, context, **kwargs) -> dict[str, Any]:
        execution_id = str(kwargs.get("execution_id") or "").strip()
        if not execution_id:
            return {"ok": False, "error": "execution_id 必填"}
        if await self._service.pause_execution(execution_id):
            return {"execution_id": execution_id, "status": "paused"}
        record = await self._service.get_execution(execution_id)
        state = record["status"] if record is not None else "unknown"
        return {"ok": False, "error": f"execution {execution_id} 状态为 {state}，不可暂停"}


class WorkflowResumeTool(SchemaWorkflowTool):
    name: str = "workflow_resume"
    description: str = (
        "从 paused 执行记录的 checkpoint 续跑（需提供 flow_id；resume_data 可注入额外变量）。"
    )
    is_read_only: bool = False
    is_idempotent: bool = False
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "execution_id": {
                "type": "string",
                "minLength": 1,
                "description": "已暂停的 execution_id",
            },
            "flow_id": {
                "type": "string",
                "minLength": 1,
                "description": "该执行对应的工作流 ID",
            },
            "resume_data": {
                "type": "object",
                "description": "续跑时注入的额外变量（并入 variables）",
            },
        },
        "required": ["execution_id", "flow_id"],
    }

    async def _execute(self, context, **kwargs) -> dict[str, Any]:
        execution_id = str(kwargs.get("execution_id") or "").strip()
        flow_id = str(kwargs.get("flow_id") or "").strip()
        if not execution_id or not flow_id:
            return {"ok": False, "error": "execution_id 与 flow_id 均必填"}
        resume_data = kwargs.get("resume_data") or {}
        if not isinstance(resume_data, dict):
            return {"ok": False, "error": "resume_data 必须是对象"}
        record = await self._service.resume_execution(
            execution_id, flow_id, resume_data=resume_data
        )
        if record is None:
            return {
                "ok": False,
                "error": f"execution {execution_id} 不存在或当前状态不可恢复（须为 paused）",
            }
        return record
