# 功能：Day134 定时任务调度器契约（B）——ScheduledTask 模型 + TaskStore 持久化 + TaskScheduler tick 语义
# 背景：调度循环复用 company/heartbeat.run_tick_loop 共享骨架（tick 容错 + interval 钳制 + stop 清理）。
# runner 注入（默认走 headless agent turn）——契约钉 tick 触发/推进/容错，不钉执行体内核。
# 契约钉行为族：store CRUD+持久化 / 到期触发·未到期静默 / disabled 跳过 / 触发后 last_run+next_run
# 推进 / 单任务异常不吞 tick / 双触发防抖（missed window 只补一次）/ interval 钳制幂等启停。
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from bandi.scheduler.cron import InvalidCronExpressionError
from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
from bandi.scheduler.store import ScheduledTask, TaskStatus, TaskStore


def make_task(**overrides: object) -> ScheduledTask:
    base: dict[str, object] = {
        "task_id": "t1",
        "name": "daily report",
        "cron": "0 9 * * *",
        "prompt": "run daily report",
        "enabled": True,
    }
    base.update(overrides)
    return ScheduledTask(**base)  # type: ignore[arg-type]


class TestTaskStoreCrud:
    """store 族：create/get/list/update/delete + sqlite 重启持久化。"""

    def test_create_and_get(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        task = make_task()
        store.save(task)
        got = store.get("t1")
        assert got is not None
        assert got.name == "daily report"
        assert got.cron == "0 9 * * *"
        assert got.enabled is True
        assert got.status == TaskStatus.IDLE

    def test_save_updates_existing(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        store.save(make_task())
        store.save(make_task(name="renamed"))
        tasks = store.list()
        assert len(tasks) == 1
        assert tasks[0].name == "renamed"

    def test_delete(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        store.save(make_task())
        store.delete("t1")
        assert store.get("t1") is None
        assert store.list() == []

    def test_persistence_across_reopen(self, tmp_path: Path) -> None:
        path = str(tmp_path / "scheduler.db")
        store = TaskStore(path)
        store.save(make_task(cron="*/5 * * * *"))
        store.close()

        reopened = TaskStore(path)
        got = reopened.get("t1")
        assert got is not None
        assert got.cron == "*/5 * * * *"

    def test_invalid_cron_rejected_on_save(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        with pytest.raises(InvalidCronExpressionError):
            store.save(make_task(cron="not a cron"))

    def test_save_initializes_next_run_when_missing(self, tmp_path: Path) -> None:
        """save 时 next_run_at 缺省 → 按给定 now 用 cron.next_after 初始化排程。"""
        store = TaskStore(str(tmp_path / "scheduler.db"))
        store.save(make_task(cron="0 9 * * *"), now=datetime(2026, 9, 5, 8, 0, 0))
        got = store.get("t1")
        assert got is not None
        assert got.next_run_at == datetime(2026, 9, 5, 9, 0, 0)

    def test_save_preserves_explicit_next_run(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        explicit = datetime(2026, 9, 7, 9, 0, 0)
        store.save(make_task(next_run_at=explicit))
        got = store.get("t1")
        assert got is not None and got.next_run_at == explicit

    def test_empty_list(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        assert store.list() == []

    def test_concurrent_singleton_store_shared(self, tmp_path: Path) -> None:
        """同路径两个 store 写同一文件应可读回（sqlite 文件级）。"""
        a = TaskStore(str(tmp_path / "scheduler.db"))
        b = TaskStore(str(tmp_path / "scheduler.db"))
        a.save(make_task())
        assert b.get("t1") is not None
        a.close()
        b.close()


class TestTaskSchedulerTick:
    """tick 语义族：到期触发 / 静默 / disabled / 推进 / 容错 / 防抖 / 生命周期。"""

    def _make(self, tmp_path: Path, runner: object) -> tuple[TaskStore, TaskScheduler]:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        sched = TaskScheduler(
            store=store,
            runner=runner,  # type: ignore[arg-type]
            config=SchedulerConfig(
                tick_interval_sec=1.0,
                now_fn=lambda: datetime(2026, 9, 5, 8, 59, 0),
                # 10.3：本文件只钉 tick 韧性/推进语义——窗口重试关闭（重试行为
                # 由 契约 test_scheduler_retries_failed_window_before_advancing 覆盖）
                max_window_retries=0,
            ),
        )
        return store, sched

    def _set_now(self, sched: TaskScheduler, moment: datetime) -> None:
        sched._config.now_fn = lambda: moment

    def test_tick_trigger_due_task(self, tmp_path: Path) -> None:
        fired: list[str] = []

        async def runner(task: ScheduledTask) -> None:
            fired.append(task.task_id)

        store, sched = self._make(tmp_path, runner)
        store.save(make_task(next_run_at=datetime(2026, 9, 5, 9, 0, 0)))
        self._set_now(sched, datetime(2026, 9, 5, 8, 59, 0))
        asyncio.run(sched._tick())
        assert fired == [], "未到期不得触发"

        self._set_now(sched, datetime(2026, 9, 5, 9, 0, 0))
        asyncio.run(sched._tick())
        assert fired == ["t1"]

    def test_tick_same_minute_no_double_fire(self, tmp_path: Path) -> None:
        """到期触发后 next_run_at 推进 → 同分钟第二次 tick 不得重复触发。"""
        fired: list[str] = []

        async def runner(task: ScheduledTask) -> None:
            fired.append(task.task_id)

        store, sched = self._make(tmp_path, runner)
        store.save(make_task(cron="* * * * *", next_run_at=datetime(2026, 9, 5, 9, 0, 0)))
        self._set_now(sched, datetime(2026, 9, 5, 9, 0, 0))
        asyncio.run(sched._tick())
        asyncio.run(sched._tick())
        assert fired == ["t1"], "同分钟重复 tick 不得二次触发"

    def test_tick_disabled_task_skipped(self, tmp_path: Path) -> None:
        fired: list[str] = []

        async def runner(task: ScheduledTask) -> None:
            fired.append(task.task_id)

        store, sched = self._make(tmp_path, runner)
        store.save(
            make_task(cron="* * * * *", enabled=False, next_run_at=datetime(2026, 9, 5, 9, 0, 0))
        )
        self._set_now(sched, datetime(2026, 9, 5, 9, 0, 0))
        asyncio.run(sched._tick())
        assert fired == []

    def test_task_run_updates_last_run_and_next_run(self, tmp_path: Path) -> None:
        fired: list[str] = []

        async def runner(task: ScheduledTask) -> None:
            fired.append(task.task_id)

        store, sched = self._make(tmp_path, runner)
        store.save(make_task(next_run_at=datetime(2026, 9, 5, 9, 0, 0)))
        self._set_now(sched, datetime(2026, 9, 5, 9, 0, 0))
        asyncio.run(sched._tick())
        task = store.get("t1")
        assert task is not None
        assert task.last_run_at == datetime(2026, 9, 5, 9, 0, 0)
        assert task.next_run_at == datetime(2026, 9, 6, 9, 0, 0)  # 推进到明天 9:00

    def test_missed_window_fires_once(self, tmp_path: Path) -> None:
        """进程错过多个窗口（sleep/重启），恢复后只补跑一次并推进到未来。"""
        fired: list[str] = []

        async def runner(task: ScheduledTask) -> None:
            fired.append(task.task_id)

        store, sched = self._make(tmp_path, runner)
        # 每分钟任务，next_run 已过（9:00），进程重启已到 9:05 → 补跑一次
        store.save(make_task(cron="* * * * *", next_run_at=datetime(2026, 9, 5, 9, 0, 0)))
        self._set_now(sched, datetime(2026, 9, 5, 9, 5, 0))
        asyncio.run(sched._tick())
        assert fired == ["t1"]
        task = store.get("t1")
        assert task is not None
        assert task.next_run_at > datetime(2026, 9, 5, 9, 5, 0)  # 推进到未来，不补连环跑

    def test_runner_error_does_not_break_tick(self, tmp_path: Path) -> None:
        """单任务 runner 异常只跳过推进 last_run（失败可见），不吞 tick，其他任务照常。"""
        calls: list[str] = []

        async def runner(task: ScheduledTask) -> None:
            calls.append(task.task_id)
            if task.task_id == "bad":
                raise RuntimeError("boom")

        store, sched = self._make(tmp_path, runner)
        store.save(
            make_task(task_id="bad", cron="* * * * *", next_run_at=datetime(2026, 9, 5, 9, 0, 0))
        )
        store.save(
            make_task(task_id="good", cron="* * * * *", next_run_at=datetime(2026, 9, 5, 9, 0, 0))
        )
        self._set_now(sched, datetime(2026, 9, 5, 9, 0, 0))
        asyncio.run(sched._tick())  # 不应抛出
        assert "bad" in calls and "good" in calls
        good = store.get("good")
        assert good is not None and good.last_run_at == datetime(2026, 9, 5, 9, 0, 0)
        bad = store.get("bad")
        assert bad is not None
        assert bad.last_run_at is None, "失败任务 last_run_at 不得置位（失败可见）"
        assert bad.next_run_at > datetime(2026, 9, 5, 9, 0, 0), (
            "失败任务也推进 next_run（防重试风暴）"
        )

    def test_lifecycle_start_stop_idempotent(self, tmp_path: Path) -> None:
        """start/stop 幂等，interval 钳制到 ≥1s。"""

        async def runner(task: ScheduledTask) -> None:
            return None

        _, sched = self._make(tmp_path, runner)

        async def _run() -> None:
            await sched.start()
            await sched.start()  # 幂等：不重复起 task
            assert sched._running
            await sched.stop()
            await sched.stop()  # 幂等
            assert not sched._running

        asyncio.run(_run())
        assert sched._config.tick_interval_sec >= 1.0

    def test_set_next_run_and_last_run_persist(self, tmp_path: Path) -> None:
        store = TaskStore(str(tmp_path / "scheduler.db"))
        store.save(make_task())
        t = datetime(2026, 9, 6, 9, 0, 0)
        store.set_next_run("t1", t)
        store.set_last_run("t1", datetime(2026, 9, 5, 9, 0, 0))
        store.close()

        reopened = TaskStore(str(tmp_path / "scheduler.db"))
        got = reopened.get("t1")
        assert got is not None
        assert got.next_run_at == t
        assert got.last_run_at == datetime(2026, 9, 5, 9, 0, 0)
