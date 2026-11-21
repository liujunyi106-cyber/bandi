"""功能：定时任务必须「投递」而非当场执行 Agent。

真机缺陷（自动化任务跑了但用户完全无感，且失败不可见）：

    调度器到期 → 直接 await run_agent_turn(headless)
    → Agent 把 LLM 失败包装成一句普通回复返回
    → last_error=None、last_output="模型服务暂时不可用"
    → 事后无法判断失败在哪一段

根因是执行路径分叉：定时任务走独立的 headless 路径（模拟交互、异常吞成回复），
与用户手动发消息的链路不同，于是有自己的一套失败面。

改为投递式后：到期只把消息写进投递箱并推进窗口，真正的 Agent 回合由
消费者走**与用户发消息同一条链路**执行。本契约钉住这条不变式。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from bandi.scheduler.consumer import DeliveryConsumer
from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
from bandi.scheduler.store import DeliveryStatus, DeliveryStore, ScheduledTask, TaskStore


def _due_task(task_id: str = "t1", session_id: str = "sess-x") -> ScheduledTask:
    return ScheduledTask(
        task_id=task_id,
        name="统计",
        cron="0 9 * * *",
        prompt="统计文件数",
        enabled=True,
        next_run_at=datetime.now() - timedelta(minutes=1),
        payload={"session_id": session_id},
    )


def _stores(tmp_path: Path) -> tuple[TaskStore, DeliveryStore]:
    db = str(tmp_path / "scheduler.db")
    return TaskStore(db), DeliveryStore(db)


def _scheduler(tasks: TaskStore, deliveries: DeliveryStore) -> TaskScheduler:
    return TaskScheduler(
        store=tasks,
        runner=lambda _task: None,
        config=SchedulerConfig(now_fn=datetime.now, maintenance_enabled=False),
        deliveries=deliveries,
    )


def test_firing_enqueues_instead_of_running(tmp_path: Path) -> None:
    """功能：到期只入箱——不得当场执行 Agent。"""
    tasks, deliveries = _stores(tmp_path)
    tasks.save(_due_task())
    executed: list[str] = []

    async def _run() -> None:
        scheduler = TaskScheduler(
            store=tasks,
            runner=lambda t: executed.append(t.task_id),  # type: ignore[func-returns-value]
            config=SchedulerConfig(now_fn=datetime.now, maintenance_enabled=False),
            deliveries=deliveries,
        )
        await scheduler._tick()

    asyncio.run(_run())

    assert executed == [], "投递式下调度器不得直接执行任务"
    pending = deliveries.claim_pending(limit=10)
    assert len(pending) == 1, "到期必须产生一条待消费投递"
    assert pending[0].task_id == "t1"
    assert pending[0].session_id == "sess-x", "投递必须携带任务绑定的会话"


def test_window_advances_even_though_execution_is_pending(tmp_path: Path) -> None:
    """功能：入箱成功即推进窗口——投递成功≠执行成功，不能卡住未来窗口。"""
    tasks, deliveries = _stores(tmp_path)
    tasks.save(_due_task())

    async def _run() -> None:
        await _scheduler(tasks, deliveries)._tick()

    asyncio.run(_run())
    task = tasks.get("t1")
    assert task is not None
    assert task.next_run_at is not None and task.next_run_at > datetime.now()
    assert task.status.value == "idle"


def test_same_window_never_enqueues_twice(tmp_path: Path) -> None:
    """功能：同一 (task, 窗口) 只入箱一次——catch-up / 重启补跑不得重复执行。"""
    tasks, deliveries = _stores(tmp_path)
    tasks.save(_due_task())
    window = datetime.now() - timedelta(minutes=1)

    async def _run() -> None:
        scheduler = _scheduler(tasks, deliveries)
        await scheduler._tick()
        # 人为把窗口拉回同一时刻，模拟重试/重启补跑
        tasks._conn.execute(
            "UPDATE scheduled_tasks SET next_run_at = ? WHERE task_id = 't1'",
            (window.strftime("%Y-%m-%dT%H:%M:%S"),),
        )
        tasks._conn.commit()
        await scheduler._tick()

    asyncio.run(_run())
    assert len(deliveries.claim_pending(limit=10)) == 1, "同一窗口不得重复入箱"


def test_consumer_executes_and_marks_consumed(tmp_path: Path) -> None:
    """功能：消费者取到投递后走执行回调，并落 consumed 终态。"""
    tasks, deliveries = _stores(tmp_path)
    tasks.save(_due_task())
    seen: list[str] = []

    async def _execute(delivery) -> str:
        seen.append(delivery.delivery_id)
        return "已完成"

    async def _run() -> int:
        await _scheduler(tasks, deliveries)._tick()
        consumer = DeliveryConsumer(deliveries=deliveries, execute=_execute)
        return await consumer.drain()

    handled = asyncio.run(_run())
    assert handled == 1 and seen, "投递必须被消费"
    assert deliveries.pending_count() == 0
    history = deliveries.list_for_task("t1")
    assert [d.status for d in history] == [DeliveryStatus.CONSUMED]


def test_failed_delivery_is_not_retried(tmp_path: Path) -> None:
    """功能：执行失败落 failed + 原因，不自动重试（防投递重试风暴）。"""
    tasks, deliveries = _stores(tmp_path)
    tasks.save(_due_task())

    async def _boom(_delivery: object) -> str:
        raise RuntimeError("模型服务暂时不可用")

    async def _run() -> int:
        await _scheduler(tasks, deliveries)._tick()
        consumer = DeliveryConsumer(deliveries=deliveries, execute=_boom)
        return await consumer.drain()

    asyncio.run(_run())
    assert deliveries.pending_count() == 0, "失败投递不得留在 pending 里反复重试"
    history = deliveries.list_for_task("t1")
    assert history[0].status == DeliveryStatus.FAILED
    assert "模型服务暂时不可用" in history[0].error


def test_no_consumer_keeps_delivery_pending(tmp_path: Path) -> None:
    """功能：无消费者时投递留在待消费状态（at-least-once，重启后仍可取到）。"""
    tasks, deliveries = _stores(tmp_path)
    tasks.save(_due_task())

    async def _run() -> None:
        await _scheduler(tasks, deliveries)._tick()

    asyncio.run(_run())
    assert deliveries.pending_count() == 1
    assert [d.status for d in deliveries.list_for_task("t1")] == [DeliveryStatus.PENDING]
