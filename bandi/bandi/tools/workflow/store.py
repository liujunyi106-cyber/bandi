# 功能：workflow 持久化——Flow（DAG 文档）与 WorkflowExecution（执行记录）sqlite 存储
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from bandi.utils.db import connect_app_db

_DT_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _now() -> str:
    return datetime.now().strftime(_DT_FORMAT)


class FlowStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class FlowType(StrEnum):
    AGENT = "agent"
    WORKFLOW = "workflow"
    CHAT = "chat"
    TOOL = "tool"


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


@dataclass
class Flow:
    flow_id: str
    name: str
    description: str | None = None
    status: FlowStatus = FlowStatus.DRAFT
    flow_type: FlowType = FlowType.WORKFLOW
    data: dict[str, Any] | None = None
    version: int = 1
    run_count: int = 0
    last_run_at: str | None = None
    settings: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class WorkflowExecution:
    execution_id: str
    flow_id: str | None = None
    user_id: str = ""
    trigger_type: str = "manual"
    status: ExecutionStatus = ExecutionStatus.PENDING
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    execution_history: list[dict[str, Any]] = field(default_factory=list)
    current_node: str | None = None
    node_count: int = 0
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    workflow_state_id: str | None = None
    state_json: dict[str, Any] | None = None
    created_at: str | None = None
    updated_at: str | None = None


_EXECUTION_UPDATE_COLUMNS = frozenset(
    {
        "status",
        "outputs",
        "execution_history",
        "node_count",
        "error",
        "completed_at",
        "duration_ms",
        "state_json",
        "current_node",
        "user_id",
        "trigger_type",
        "inputs",
    }
)


def _serialize(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _deserialize(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


class WorkflowStore:
    # 功能：Flow/WorkflowExecution 的 sqlite 存取——产品面与 Agent 面共用同一存储

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn = connect_app_db(self._path, row_factory=True)
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS flows (
                flow_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                flow_type TEXT NOT NULL,
                data TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                run_count INTEGER NOT NULL DEFAULT 0,
                last_run_at TEXT,
                settings TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS executions (
                execution_id TEXT PRIMARY KEY,
                flow_id TEXT,
                user_id TEXT,
                trigger_type TEXT NOT NULL DEFAULT 'manual',
                status TEXT NOT NULL,
                inputs TEXT,
                outputs TEXT,
                execution_history TEXT,
                current_node TEXT,
                node_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TEXT,
                completed_at TEXT,
                duration_ms INTEGER,
                workflow_state_id TEXT,
                state_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save_flow(self, flow: Flow) -> None:
        now = _now()
        existing = self._conn.execute(
            "SELECT created_at FROM flows WHERE flow_id = ?", (flow.flow_id,)
        ).fetchone()
        row = {
            "flow_id": flow.flow_id,
            "name": flow.name,
            "description": flow.description,
            "status": flow.status.value,
            "flow_type": flow.flow_type.value,
            "data": _serialize(flow.data),
            "version": flow.version,
            "run_count": flow.run_count,
            "last_run_at": flow.last_run_at,
            "settings": _serialize(flow.settings),
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
        }
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self._conn.execute(
            f"INSERT OR REPLACE INTO flows ({columns}) VALUES ({placeholders})",
            [row[key] for key in row],
        )
        self._conn.commit()

    def get_flow(self, flow_id: str) -> Flow | None:
        row = self._conn.execute("SELECT * FROM flows WHERE flow_id = ?", (flow_id,)).fetchone()
        return self._flow_from_row(row) if row is not None else None

    def list_flows(
        self,
        *,
        flow_type: str | FlowType | None = None,
        status: str | FlowStatus | None = None,
        limit: int = 20,
    ) -> list[Flow]:
        clauses, args = [], []
        if flow_type is not None:
            clauses.append("flow_type = ?")
            args.append(flow_type.value if isinstance(flow_type, FlowType) else flow_type)
        if status is not None:
            clauses.append("status = ?")
            args.append(status.value if isinstance(status, FlowStatus) else status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM flows {where} ORDER BY created_at ASC, rowid ASC LIMIT ?",
            args + [limit],
        ).fetchall()
        return [self._flow_from_row(row) for row in rows]

    def mark_flow_run(self, flow_id: str, at: str | None = None) -> None:
        now = at or _now()
        self._conn.execute(
            "UPDATE flows SET run_count = run_count + 1, last_run_at = ?, updated_at = ? "
            "WHERE flow_id = ?",
            (now, now, flow_id),
        )
        self._conn.commit()

    def create_execution(self, execution: WorkflowExecution) -> WorkflowExecution:
        now = _now()
        row = {
            "execution_id": execution.execution_id,
            "flow_id": execution.flow_id,
            "user_id": execution.user_id,
            "trigger_type": execution.trigger_type,
            "status": execution.status.value,
            "inputs": _serialize(execution.inputs),
            "outputs": _serialize(execution.outputs),
            "execution_history": _serialize(execution.execution_history),
            "current_node": execution.current_node,
            "node_count": execution.node_count,
            "error": execution.error,
            "started_at": execution.started_at,
            "completed_at": execution.completed_at,
            "duration_ms": execution.duration_ms,
            "workflow_state_id": execution.workflow_state_id,
            "state_json": _serialize(execution.state_json),
            "created_at": execution.created_at or now,
            "updated_at": execution.updated_at or now,
        }
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self._conn.execute(
            f"INSERT INTO executions ({columns}) VALUES ({placeholders})",
            [row[key] for key in row],
        )
        self._conn.commit()
        execution.created_at = row["created_at"]
        execution.updated_at = row["updated_at"]
        return execution

    def get_execution(self, execution_id: str) -> WorkflowExecution | None:
        row = self._conn.execute(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return self._execution_from_row(row) if row is not None else None

    def update_execution(self, execution_id: str, **fields: Any) -> WorkflowExecution | None:
        current = self.get_execution(execution_id)
        if current is None:
            return None
        # 非法 SQL 列被 sqlite 兜底报错/未来新列注入）
        unknown = [k for k in fields if k not in _EXECUTION_UPDATE_COLUMNS]
        if unknown:
            raise ValueError(f"update_execution 非法字段: {sorted(unknown)}")
        serialized = {key: _serialize(value) for key, value in fields.items()}
        serialized["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in serialized)
        self._conn.execute(
            f"UPDATE executions SET {assignments} WHERE execution_id = ?",
            [serialized[key] for key in serialized] + [execution_id],
        )
        self._conn.commit()
        return self.get_execution(execution_id)

    def list_executions(
        self, flow_id: str | None = None, limit: int = 20
    ) -> list[WorkflowExecution]:
        if flow_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM executions WHERE flow_id = ? ORDER BY rowid ASC LIMIT ?",
                (flow_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM executions ORDER BY rowid ASC LIMIT ?", (limit,)
            ).fetchall()
        return [self._execution_from_row(row) for row in rows]

    def _flow_from_row(self, row: sqlite3.Row) -> Flow:
        return Flow(
            flow_id=row["flow_id"],
            name=row["name"],
            description=row["description"],
            status=FlowStatus(row["status"]),
            flow_type=FlowType(row["flow_type"]),
            data=_deserialize(row["data"]),
            version=row["version"],
            run_count=row["run_count"],
            last_run_at=row["last_run_at"],
            settings=_deserialize(row["settings"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _execution_from_row(self, row: sqlite3.Row) -> WorkflowExecution:
        return WorkflowExecution(
            execution_id=row["execution_id"],
            flow_id=row["flow_id"],
            user_id=row["user_id"],
            trigger_type=row["trigger_type"],
            status=ExecutionStatus(row["status"]),
            inputs=_deserialize(row["inputs"]) or {},
            outputs=_deserialize(row["outputs"]) or {},
            execution_history=_deserialize(row["execution_history"]) or [],
            current_node=row["current_node"],
            node_count=row["node_count"],
            error=row["error"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            duration_ms=row["duration_ms"],
            workflow_state_id=row["workflow_state_id"],
            state_json=_deserialize(row["state_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
