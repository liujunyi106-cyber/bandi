# 功能：WorkflowService——工作流编排：run/get/list/save/cancel/pause/resume + DAG 执行持久化
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from bandi.llm.base import BaseProvider
from bandi.tools.workflow.engine import NodeExecutionError, WorkflowEngine
from bandi.tools.workflow.store import (
    ExecutionStatus,
    Flow,
    FlowStatus,
    FlowType,
    WorkflowExecution,
    WorkflowStore,
    _now,
)


class WorkflowService:
    # 功能：串联 store 与 engine——每次运行落库、LLM/工具缺失显式报错、暂停按 checkpoint 续跑

    def __init__(
        self,
        store: WorkflowStore,
        engine: WorkflowEngine | None = None,
        provider: BaseProvider | None = None,
        tool_runner: Any = None,
    ) -> None:
        self._store = store
        self._engine = engine or WorkflowEngine()
        self._provider = provider
        self._tool_runner = tool_runner

    def list_flows(
        self,
        flow_type: str | FlowType | None = None,
        status: str | FlowStatus | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        flows = self._store.list_flows(
            flow_type=_as_enum(flow_type, FlowType),
            status=_as_enum(status, FlowStatus),
            limit=int(limit) or 20,
        )
        return [self._flow_view(flow) for flow in flows]

    async def start(
        self,
        flow_id: str,
        user_id: str = "",
        inputs: dict[str, Any] | None = None,
        trigger_type: str = "manual",
    ) -> dict[str, Any]:
        flow = self._store.get_flow(flow_id)
        if flow is None:
            return {"ok": False, "error": f"Flow {flow_id} not found"}
        if not flow.data:
            return {"ok": False, "error": f"Flow {flow_id} 缺少 data（还未保存 DAG 文档）"}

        execution_id = uuid.uuid4().hex
        started_at = _now()
        inputs = dict(inputs or {})
        self._store.create_execution(
            WorkflowExecution(
                execution_id=execution_id,
                flow_id=flow_id,
                user_id=user_id,
                trigger_type=trigger_type,
                status=ExecutionStatus.RUNNING,
                inputs=inputs,
                started_at=started_at,
            )
        )
        self._store.mark_flow_run(flow_id, at=started_at)

        async def should_cancel() -> bool:
            record = self._store.get_execution(execution_id)
            return record is not None and record.status == ExecutionStatus.CANCELLED

        async def should_pause() -> bool:
            record = self._store.get_execution(execution_id)
            return record is not None and record.status == ExecutionStatus.PAUSED

        result = await self._engine.execute(
            flow.data,
            inputs=inputs,
            provider=self._provider,
            tool_runner=self._tool_runner,
            should_cancel=should_cancel,
            should_pause=should_pause,
        )
        self._store.update_execution(
            execution_id,
            status=result.status,
            outputs=result.outputs,
            execution_history=result.execution_history,
            node_count=len(result.execution_history),
            duration_ms=result.duration_ms,
            error="; ".join(result.errors) or None,
            completed_at=_now(),
            state_json=result.checkpoint,
        )
        return {
            "execution_id": execution_id,
            "status": result.status,
            "outputs": result.outputs,
            "errors": result.errors,
            "duration_ms": result.duration_ms,
        }

    async def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        record = self._store.get_execution(execution_id)
        return self._execution_view(record) if record is not None else None

    async def list_executions(
        self, flow_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        records = self._store.list_executions(flow_id=flow_id, limit=limit)
        return [self._execution_view(record) for record in records]

    async def cancel_execution(self, execution_id: str) -> bool:
        record = self._store.get_execution(execution_id)
        if record is None or record.status not in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.PENDING,
        ):
            return False
        self._store.update_execution(execution_id, status=ExecutionStatus.CANCELLED, error=None)
        return True

    async def pause_execution(self, execution_id: str) -> bool:
        record = self._store.get_execution(execution_id)
        if record is None or record.status not in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.PENDING,
        ):
            return False
        self._store.update_execution(execution_id, status=ExecutionStatus.PAUSED)
        return True

    async def resume_execution(
        self,
        execution_id: str,
        flow_id: str,
        resume_data: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        record = self._store.get_execution(execution_id)
        flow = self._store.get_flow(flow_id)
        if record is None or flow is None or not flow.data:
            return None
        if record.status != ExecutionStatus.PAUSED:
            return None
        checkpoint = dict(record.state_json or {})
        if resume_data:
            variables = dict(checkpoint.get("variables") or {})
            variables.update(resume_data)
            checkpoint["variables"] = variables
        self._store.update_execution(execution_id, status=ExecutionStatus.RUNNING)

        async def should_cancel() -> bool:
            current = self._store.get_execution(execution_id)
            return current is not None and current.status == ExecutionStatus.CANCELLED

        async def should_pause() -> bool:
            current = self._store.get_execution(execution_id)
            return current is not None and current.status == ExecutionStatus.PAUSED

        result = await self._engine.execute(
            flow.data,
            inputs=record.inputs,
            provider=self._provider,
            tool_runner=self._tool_runner,
            resume_state=checkpoint,
            should_cancel=should_cancel,
            should_pause=should_pause,
        )
        self._store.update_execution(
            execution_id,
            status=result.status,
            outputs=result.outputs,
            execution_history=result.execution_history,
            node_count=len(result.execution_history),
            duration_ms=result.duration_ms,
            error="; ".join(result.errors) or None,
            completed_at=_now(),
            state_json=result.checkpoint,
        )
        return {
            "execution_id": execution_id,
            "status": result.status,
            "outputs": result.outputs,
            "execution_history": result.execution_history,
            "errors": result.errors,
            "duration_ms": result.duration_ms,
        }

    def save_flow(
        self,
        name: str,
        flow_data: dict[str, Any],
        description: str = "",
        flow_id: str | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        # 功能：校验 DAG 并落库（INSERT OR REPLACE 天然支持更新），返回 flow 摘要
        try:
            node_count = self._engine.validate(flow_data)
        except NodeExecutionError as exc:
            return {"ok": False, "error": f"Invalid workflow graph: {exc}"}
        resolved_id = flow_id or uuid.uuid4().hex
        digest = hashlib.sha256(
            json.dumps(flow_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self._store.save_flow(
            Flow(
                flow_id=resolved_id,
                name=name,
                description=description or None,
                status=FlowStatus.PUBLISHED if publish else FlowStatus.DRAFT,
                flow_type=FlowType.WORKFLOW,
                data=flow_data,
            )
        )
        status = FlowStatus.PUBLISHED if publish else FlowStatus.DRAFT
        return {
            "flow_id": resolved_id,
            "name": name,
            "node_count": node_count,
            "status": status.value,
            "digest": digest,
        }

    def _execution_view(self, record: WorkflowExecution) -> dict[str, Any]:
        return {
            "execution_id": record.execution_id,
            "flow_id": record.flow_id,
            "user_id": record.user_id,
            "trigger_type": record.trigger_type,
            "status": record.status.value,
            "inputs": record.inputs,
            "outputs": record.outputs,
            "execution_history": record.execution_history,
            "current_node": record.current_node,
            "node_count": record.node_count,
            "error": record.error,
            "started_at": record.started_at,
            "completed_at": record.completed_at,
            "duration_ms": record.duration_ms,
            "workflow_state_id": record.workflow_state_id,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def _flow_view(self, flow) -> dict[str, Any]:
        return {
            "id": flow.flow_id,
            "name": flow.name,
            "description": flow.description,
            "status": flow.status.value,
            "flow_type": flow.flow_type.value,
            "version": flow.version,
            "run_count": flow.run_count,
            "last_run_at": flow.last_run_at,
            "settings": flow.settings,
        }


def _as_enum(value, enum_type) -> Any:
    if value is None:
        return None
    if isinstance(value, enum_type):
        return value
    return enum_type(str(value))
