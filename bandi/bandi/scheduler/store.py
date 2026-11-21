# 功能：定时任务模型 + sqlite 持久化（独立 scheduler 包）
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from bandi.scheduler.cron import CronExpr
from bandi.utils.db import connect_app_db

logger = logging.getLogger(__name__)

_DT_FORMAT = "%Y-%m-%dT%H:%M:%S"


class TaskStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"


def _dt_to_str(moment: datetime | None) -> str | None:
    return moment.strftime(_DT_FORMAT) if moment is not None else None


def _str_to_dt(text: str | None) -> datetime | None:
    return datetime.strptime(text, _DT_FORMAT) if text else None


@dataclass
class ScheduledTask:
    """单条定时任务：cron 表达式 + 触发时执行的 prompt（headless agent turn）。"""

    task_id: str
    name: str
    cron: str
    prompt: str
    enabled: bool = True
    status: TaskStatus = TaskStatus.IDLE
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    created_at: datetime | None = None
    last_error: str | None = None
    # 10.3：静默完成协议——空/HEARTBEAT_OK 输出落空串（用户面不可见）
    last_output: str = ""
    target_type: str = ""
    target_id: str = ""
    description: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    # 功能：调度模型——cron / periodic / interval / once + 生效区间
    schedule_kind: str = "cron"
    schedule_spec: dict[str, Any] = field(default_factory=dict)
    effective_from: str = ""
    effective_until: str = ""


class TaskStore:
    """定时任务存储（独立 sqlite：~/.bandi/data/scheduler.db 或测试注入路径）。"""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_app_db(self._path, row_factory=True)
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                task_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cron TEXT NOT NULL,
                prompt TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'idle',
                next_run_at TEXT,
                last_run_at TEXT,
                created_at TEXT,
                last_error TEXT,
                last_output TEXT NOT NULL DEFAULT '',
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '',
                schedule_kind TEXT NOT NULL DEFAULT 'cron',
                schedule_spec TEXT NOT NULL DEFAULT '',
                effective_from TEXT NOT NULL DEFAULT '',
                effective_until TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 迁移：旧库补列（幂等）——last_output（10.3）+ cron 工具面元数据（cron 桥接）
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(scheduled_tasks)")}
        for column, ddl in (
            ("last_output", "TEXT NOT NULL DEFAULT ''"),
            ("target_type", "TEXT NOT NULL DEFAULT ''"),
            ("target_id", "TEXT NOT NULL DEFAULT ''"),
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("payload", "TEXT NOT NULL DEFAULT ''"),
            ("schedule_kind", "TEXT NOT NULL DEFAULT 'cron'"),
            ("schedule_spec", "TEXT NOT NULL DEFAULT ''"),
            ("effective_from", "TEXT NOT NULL DEFAULT ''"),
            ("effective_until", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in cols:
                self._conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {column} {ddl}")
        self._conn.commit()

    # -- CRUD ---------------------------------------------------------------

    def save(self, task: ScheduledTask, *, now: datetime | None = None) -> ScheduledTask:
        """插入/更新。cron 任务校验表达式；非 cron 任务按规则引擎算下一次。"""
        now_stamp = now or datetime.now()
        kind = str(task.schedule_kind or "cron").strip() or "cron"
        resolved_next = task.next_run_at
        if resolved_next is None:
            base = now_stamp
            if kind == "cron":
                expr = CronExpr(task.cron)  # 校验（非法抛 InvalidCronExpressionError）
                resolved_next = expr.next_after(base)
            else:
                from bandi.scheduler.rules import compute_next_run

                resolved_next = compute_next_run(
                    kind=kind,
                    spec=dict(task.schedule_spec or {}),
                    base=base,
                    effective_from=task.effective_from,
                    effective_until=task.effective_until,
                    anchor=(task.created_at or now_stamp).date(),
                )
        elif kind == "cron":
            CronExpr(task.cron)  # 显式 next 也必须校验 cron
        self._conn.execute(
            """
            INSERT INTO scheduled_tasks
                (task_id, name, cron, prompt, enabled, status,
                 next_run_at, last_run_at, created_at, last_error, last_output,
                 target_type, target_id, description, payload,
                 schedule_kind, schedule_spec, effective_from, effective_until)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(task_id) DO UPDATE SET
                name=excluded.name, cron=excluded.cron, prompt=excluded.prompt,
                enabled=excluded.enabled, status=excluded.status,
                next_run_at=excluded.next_run_at, last_run_at=excluded.last_run_at,
                last_error=excluded.last_error, last_output=excluded.last_output,
                target_type=excluded.target_type, target_id=excluded.target_id,
                description=excluded.description, payload=excluded.payload,
                schedule_kind=excluded.schedule_kind, schedule_spec=excluded.schedule_spec,
                effective_from=excluded.effective_from, effective_until=excluded.effective_until
            """,
            (
                task.task_id,
                task.name,
                task.cron,
                task.prompt,
                int(task.enabled),
                task.status.value,
                _dt_to_str(resolved_next),
                _dt_to_str(task.last_run_at),
                _dt_to_str(task.created_at or now_stamp),
                task.last_error,
                task.last_output,
                task.target_type,
                task.target_id,
                task.description,
                json.dumps(task.payload or {}, ensure_ascii=False),
                kind,
                json.dumps(task.schedule_spec or {}, ensure_ascii=False),
                task.effective_from,
                task.effective_until,
            ),
        )
        self._conn.commit()
        return self.get(task.task_id) or task

    def get(self, task_id: str) -> ScheduledTask | None:
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    def list(self) -> list[ScheduledTask]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_tasks ORDER BY created_at, task_id"
        ).fetchall()
        tasks: list[ScheduledTask] = []
        for row in rows:
            try:
                tasks.append(self._row_to_task(row))
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("scheduled task row %s skipped (corrupt): %s", row["task_id"], exc)
        return tasks

    def delete(self, task_id: str) -> None:
        self._conn.execute("DELETE FROM scheduled_tasks WHERE task_id = ?", (task_id,))
        self._conn.commit()

    # -- runtime state ------------------------------------------------------

    def set_next_run(self, task_id: str, moment: datetime | None) -> None:
        self._conn.execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE task_id = ?",
            (_dt_to_str(moment), task_id),
        )
        self._conn.commit()

    def set_last_run(self, task_id: str, moment: datetime) -> None:
        self._conn.execute(
            "UPDATE scheduled_tasks SET last_run_at = ? WHERE task_id = ?",
            (_dt_to_str(moment), task_id),
        )
        self._conn.commit()

    def set_enabled(self, task_id: str, enabled: bool) -> None:
        """功能：单次任务执行后或生效区间结束时停用任务。"""
        self._conn.execute(
            "UPDATE scheduled_tasks SET enabled = ? WHERE task_id = ?",
            (1 if enabled else 0, task_id),
        )
        self._conn.commit()

    def set_status(self, task_id: str, status: TaskStatus, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE scheduled_tasks SET status = ?, last_error = ? WHERE task_id = ?",
            (status.value, error, task_id),
        )
        self._conn.commit()

    def try_begin_run(self, task_id: str) -> bool:
        """原子占位运行——仅当任务当前非 running 时置 running。

        返回是否抢占成功；并发触发（手动 API 与自动 tick）中只有一个成功
        防同一任务双跑。"""
        cursor = self._conn.execute(
            "UPDATE scheduled_tasks SET status = ? WHERE task_id = ? AND status <> ?",
            (TaskStatus.RUNNING.value, task_id, TaskStatus.RUNNING.value),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def set_last_output(self, task_id: str, output: str) -> None:
        """功能（10.3）：记录最近一次运行产出（静默完成写空串，用户面不可见）。"""
        self._conn.execute(
            "UPDATE scheduled_tasks SET last_output = ? WHERE task_id = ?",
            (output, task_id),
        )
        self._conn.commit()

    def _row_to_task(self, row: sqlite3.Row) -> ScheduledTask:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        try:
            spec = json.loads(row["schedule_spec"] or "{}")
        except (TypeError, ValueError):
            spec = {}
        return ScheduledTask(
            task_id=row["task_id"],
            name=row["name"],
            cron=row["cron"],
            prompt=row["prompt"],
            enabled=bool(row["enabled"]),
            status=TaskStatus(row["status"]),
            next_run_at=_str_to_dt(row["next_run_at"]),
            last_run_at=_str_to_dt(row["last_run_at"]),
            created_at=_str_to_dt(row["created_at"]),
            last_error=row["last_error"],
            last_output=row["last_output"] or "",
            target_type=row["target_type"] or "",
            target_id=row["target_id"] or "",
            description=row["description"] or "",
            payload=payload if isinstance(payload, dict) else {},
            schedule_kind=row["schedule_kind"] or "cron",
            schedule_spec=spec if isinstance(spec, dict) else {},
            effective_from=row["effective_from"] or "",
            effective_until=row["effective_until"] or "",
        )

    def close(self) -> None:
        """功能：释放底层连接（契约与测试的收尾口径，与 DeliveryStore 一致）。"""
        self._conn.close()


class DeliveryStatus(StrEnum):
    """功能：投递状态机——pending（已入箱待消费）/ consumed / failed。"""

    PENDING = "pending"
    CONSUMED = "consumed"
    FAILED = "failed"


@dataclass
class TaskDelivery:
    """功能：一次「定时任务到期」的投递记录（含 task/session 绑定与结果）。"""

    delivery_id: str
    task_id: str
    session_id: str
    prompt: str
    scheduled_at: datetime | None = None
    delivered_at: datetime | None = None
    status: DeliveryStatus = DeliveryStatus.PENDING
    consumed_at: datetime | None = None
    error: str = ""


class DeliveryStore:
    """功能：定时任务投递箱——调度器只入箱，执行由会话侧消费。

    与 `TaskStore` 同库不同表：调度器到期时写一行 pending（幂等、可审计），
    真正的 Agent 回合由消费方走用户发消息的同一条链路完成。
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_app_db(self._path, row_factory=True)
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_deliveries (
                delivery_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                scheduled_at TEXT,
                delivered_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                consumed_at TEXT,
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_deliveries_pending "
            "ON task_deliveries (status, delivered_at)"
        )
        self._conn.commit()

    def enqueue(
        self,
        *,
        task_id: str,
        session_id: str,
        prompt: str,
        scheduled_at: datetime | None,
        now: datetime | None = None,
    ) -> str:
        """功能：投递入箱——调度器到期时调用，返回 delivery_id。

        同一 (task_id, scheduled_at) 只入箱一次：catch-up 重试、重启补跑都不得
        产生重复投递（否则同一窗口会被执行多遍）。
        """
        stamp = now or datetime.now()
        window = _dt_to_str(scheduled_at) or _dt_to_str(stamp)
        existing = self._conn.execute(
            "SELECT delivery_id FROM task_deliveries WHERE task_id = ? AND scheduled_at = ?",
            (task_id, window),
        ).fetchone()
        if existing is not None:
            return str(existing["delivery_id"])
        delivery_id = f"{task_id}:{window}"
        self._conn.execute(
            """INSERT INTO task_deliveries
                   (delivery_id, task_id, session_id, prompt, scheduled_at, delivered_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                delivery_id,
                task_id,
                session_id,
                prompt,
                window,
                _dt_to_str(stamp),
                DeliveryStatus.PENDING.value,
            ),
        )
        self._conn.commit()
        return delivery_id

    def claim_pending(self, *, limit: int = 1) -> list[TaskDelivery]:
        """功能：取待消费投递并原子置为 running 语义（本实现直接返回 pending 集合）。

        消费方执行完必须调 `mark_consumed` / `mark_failed`；未标记的投递
        保持 pending，重启后会被重新取到（at-least-once）。
        """
        rows = self._conn.execute(
            "SELECT * FROM task_deliveries WHERE status = ? ORDER BY delivered_at LIMIT ?",
            (DeliveryStatus.PENDING.value, max(1, limit)),
        ).fetchall()
        return [self._row_to_delivery(row) for row in rows]

    def mark_consumed(self, delivery_id: str, *, now: datetime | None = None) -> None:
        """功能：投递已执行完成——落终态，不再被取到。"""
        self._conn.execute(
            "UPDATE task_deliveries SET status = ?, consumed_at = ?, error = '' "
            "WHERE delivery_id = ?",
            (DeliveryStatus.CONSUMED.value, _dt_to_str(now or datetime.now()), delivery_id),
        )
        self._conn.commit()

    def mark_failed(self, delivery_id: str, error: str, *, now: datetime | None = None) -> None:
        """功能：投递执行失败——落终态与原因，不自动重试（防投递重试风暴）。"""
        self._conn.execute(
            "UPDATE task_deliveries SET status = ?, consumed_at = ?, error = ? "
            "WHERE delivery_id = ?",
            (
                DeliveryStatus.FAILED.value,
                _dt_to_str(now or datetime.now()),
                str(error)[:2000],
                delivery_id,
            ),
        )
        self._conn.commit()

    def list_for_task(self, task_id: str, *, limit: int = 20) -> list[TaskDelivery]:
        """功能：某任务的投递历史（新到旧），供界面与排障消费。"""
        rows = self._conn.execute(
            "SELECT * FROM task_deliveries WHERE task_id = ? ORDER BY delivered_at DESC LIMIT ?",
            (task_id, max(1, limit)),
        ).fetchall()
        return [self._row_to_delivery(row) for row in rows]

    def pending_count(self) -> int:
        """功能：待消费投递数（健康度指标）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM task_deliveries WHERE status = ?",
            (DeliveryStatus.PENDING.value,),
        ).fetchone()
        return int(row["n"] if row else 0)

    def _row_to_delivery(self, row: sqlite3.Row) -> TaskDelivery:
        return TaskDelivery(
            delivery_id=row["delivery_id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            prompt=row["prompt"],
            scheduled_at=_str_to_dt(row["scheduled_at"]),
            delivered_at=_str_to_dt(row["delivered_at"]),
            status=DeliveryStatus(row["status"]),
            consumed_at=_str_to_dt(row["consumed_at"]),
            error=row["error"] or "",
        )

    def close(self) -> None:
        self._conn.close()
