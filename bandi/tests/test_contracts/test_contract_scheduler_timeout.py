# 功能：调度器任务超时契约——单个挂死任务不得阻塞整个 tick（真实缺陷收口）
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
from bandi.scheduler.store import ScheduledTask, TaskStatus, TaskStore

_NOW = datetime(2026, 9, 5, 9, 0, 0)


def _save(store: TaskStore, task_id: str) -> None:
    store.save(
        ScheduledTask(
            task_id=task_id,
            name=task_id,
            cron="* * * * *",
            prompt="p",
            next_run_at=_NOW,
        )
    )


def test_hanging_task_times_out_and_tick_continues(tmp_path: Path) -> None:
    """挂死任务超时判 FAILED；同 tick 后续任务照常执行（tick 不被阻塞）。"""
    ran: list[str] = []

    async def runner(task: ScheduledTask) -> None:
        if task.task_id == "hang":
            await asyncio.sleep(60)
        ran.append(task.task_id)

    store = TaskStore(str(tmp_path / "scheduler.db"))
    _save(store, "hang")
    _save(store, "good")
    sched = TaskScheduler(
        store=store,
        runner=runner,
        config=SchedulerConfig(now_fn=lambda: _NOW, task_timeout_sec=0.05, max_window_retries=0),
    )

    asyncio.run(sched._tick())

    hang = store.get("hang")
    assert hang is not None
    assert hang.status == TaskStatus.FAILED, "挂死任务必须判失败（不能停在 RUNNING）"
    assert "timeout" in (hang.last_error or "")
    assert hang.last_run_at is None, "超时任务不得置 last_run（失败可见）"
    assert hang.next_run_at is not None and hang.next_run_at > _NOW, "超时后仍推进防重试风暴"
    assert ran == ["good"], "挂死任务之后的任务必须照常执行"


def test_fast_task_unaffected_by_timeout(tmp_path: Path) -> None:
    """正常任务不受超时配置影响（成功路径语义不变）。"""
    ran: list[str] = []

    async def runner(task: ScheduledTask) -> None:
        ran.append(task.task_id)

    store = TaskStore(str(tmp_path / "scheduler.db"))
    _save(store, "t1")
    sched = TaskScheduler(
        store=store,
        runner=runner,
        config=SchedulerConfig(now_fn=lambda: _NOW, task_timeout_sec=5.0),
    )
    asyncio.run(sched._tick())
    task = store.get("t1")
    assert ran == ["t1"]
    assert task is not None and task.status == TaskStatus.IDLE
    assert task.last_run_at == _NOW
