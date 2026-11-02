# 功能：契约——cron 工具与 TaskScheduler 桥接（存储唯一事实源 + 调度器拾取执行）
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from bandi.scheduler.scheduler import SchedulerConfig, TaskScheduler
from bandi.scheduler.store import TaskStore
from bandi.tools.base import ToolPermissionContext
from bandi.tools.util.cron_tools import CronCreateTool, CronListTool


def _ctx(root: Path) -> ToolPermissionContext:
    return ToolPermissionContext(
        user_id="unit-test", role="tester", mode="execute", extra={"project_root": str(root)}
    )


@pytest.mark.asyncio
async def test_cron_create_writes_task_store(tmp_path: Path) -> None:
    """契约（cron 桥接 · 存储唯一事实源）：cron_create 必须写入 TaskStore——
    此前写 UtilStore.cron_jobs（独立存储），TaskScheduler 读不到（store-only 缺口）。"""
    store = TaskStore(str(tmp_path / "scheduler.db"))
    tool = CronCreateTool(store=store)
    result = await tool.execute(
        {
            "name": "每日总结",
            "schedule": "0 0 * * *",
            "prompt": "总结今天的会话",
            "target_type": "task",
        },
        _ctx(tmp_path),
    )
    assert result["ok"] is True
    task_id = result["job"]["id"]
    saved = store.get(task_id)
    assert saved is not None, "任务必须落在 TaskStore（调度器可拾取）"
    assert saved.cron == "0 0 * * *"
    assert saved.prompt == "总结今天的会话"
    assert saved.next_run_at is not None, "保存时须初始化排程（next_run_at）"


@pytest.mark.asyncio
async def test_cron_task_fired_by_scheduler(tmp_path: Path) -> None:
    """契约（cron 桥接 · e2e 触发）：cron_create 登记的任务必须能被 TaskScheduler
    到期拾取执行（runner 收到以 prompt 承载的任务）——打通「工具登记→后台触发」全链。"""
    store = TaskStore(str(tmp_path / "scheduler.db"))
    fired: list[str] = []

    async def runner(task: Any) -> None:
        fired.append(task.prompt)

    now = datetime(2026, 9, 17, 0, 0, 0)
    scheduler = TaskScheduler(
        store, runner, SchedulerConfig(now_fn=lambda: now, tick_interval_sec=1.0)
    )
    tool = CronCreateTool(store=store)
    result = await tool.execute(
        {"name": "整点", "schedule": "* * * * *", "prompt": "ping"}, _ctx(tmp_path)
    )
    task_id = result["job"]["id"]
    # 未到点不触发
    store.set_next_run(task_id, datetime(2026, 9, 17, 0, 1, 0))
    await scheduler._tick()
    assert fired == []
    # 到点触发
    store.set_next_run(task_id, datetime(2026, 9, 16, 23, 59, 0))
    await scheduler._tick()
    assert fired == ["ping"], "TaskScheduler 必须拾取并执行 cron 任务"


@pytest.mark.asyncio
async def test_cron_prompt_synthesized_without_explicit(tmp_path: Path) -> None:
    """契约（cron 桥接 · prompt 合成）：未显式给 prompt 时按 target 元数据合成
    可执行描述（headless agent turn 能基于描述行动），不产生空 prompt 任务。"""
    store = TaskStore(str(tmp_path / "scheduler.db"))
    tool = CronCreateTool(store=store)
    result = await tool.execute(
        {
            "name": "跑工作流",
            "schedule": "0 8 * * *",
            "target_type": "workflow",
            "target_id": "wf-1",
            "payload": {"key": "值"},
        },
        _ctx(tmp_path),
    )
    saved = store.get(result["job"]["id"])
    assert saved is not None and saved.prompt.strip(), "必须合成非空 prompt"
    assert "workflow" in saved.prompt and "wf-1" in saved.prompt


@pytest.mark.asyncio
async def test_cron_list_reads_task_store_with_metadata(tmp_path: Path) -> None:
    """契约（cron 桥接 · 读路径）：cron_list 从 TaskStore 读回并保留
    target_type/enabled 过滤与旧 job 形态字段（id/name/schedule/status）。"""
    store = TaskStore(str(tmp_path / "scheduler.db"))
    create = CronCreateTool(store=store)
    await create.execute(
        {"name": "a", "schedule": "0 0 * * *", "target_type": "task", "prompt": "p-a"},
        _ctx(tmp_path),
    )
    await create.execute(
        {
            "name": "b",
            "schedule": "0 0 * * *",
            "target_type": "script",
            "prompt": "p-b",
            "enabled": False,
        },
        _ctx(tmp_path),
    )
    listed = await CronListTool(store=store).execute({}, _ctx(tmp_path))
    assert listed["count"] == 2
    assert {j["name"] for j in listed["jobs"]} == {"a", "b"}
    filtered = await CronListTool(store=store).execute({"target_type": "script"}, _ctx(tmp_path))
    assert filtered["count"] == 1 and filtered["jobs"][0]["name"] == "b"
