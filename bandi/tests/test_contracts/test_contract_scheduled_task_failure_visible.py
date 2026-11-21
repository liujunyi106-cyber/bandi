"""功能：定时任务失败必须留下可诊断的原因，不能只有一句用户文案。

真机缺陷（自动化任务「每日统计 py 文件数量」09:00 触发后从未产出文件）：

    last_run_at   2026-11-21T09:00:12
    last_error    None                                  ← 看起来一切正常
    last_output   模型服务暂时不可用，本轮没有产出回复。   ← 唯一线索

排查时无法判断失败在哪一段：runner 没有抛异常（否则 last_error 会有值），
Agent 把 LLM 失败包装成一句正常回复返回了，而 `_fire` 只把「抛异常」记进
last_error，`last_output` 只是结果文本。日志清理后完全无法复盘。

契约钉住：runner 返回失败结论时，必须把它落进 last_error（状态 FAILED）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bandi.scheduler.scheduler import (
    SchedulerConfig,
    TaskScheduler,
    _failure_reason,
)
from bandi.scheduler.store import ScheduledTask, TaskStatus, TaskStore


def _make_store(tmp_path: Path) -> TaskStore:
    return TaskStore(str(tmp_path / "scheduler.db"))


def _due_task() -> ScheduledTask:
    return ScheduledTask(
        task_id="t1",
        name="统计任务",
        cron="0 9 * * *",
        prompt="统计文件数",
        enabled=True,
        next_run_at=datetime.now() - timedelta(minutes=1),
    )


def _run_once(store: TaskStore, runner) -> None:
    async def _go() -> None:
        scheduler = TaskScheduler(
            store=store,
            runner=runner,
            config=SchedulerConfig(now_fn=datetime.now, maintenance_enabled=False),
        )
        await scheduler._tick()

    asyncio.run(_go())


def test_failure_text_lands_in_last_error(tmp_path: Path) -> None:
    """功能：Agent 把失败包装成正常回复时，原因必须落进 last_error。"""
    store = _make_store(tmp_path)
    store.save(_due_task())

    async def _runner(_task: object) -> str:
        return "模型服务暂时不可用，本轮没有产出回复。请稍后重试。"

    _run_once(store, _runner)

    task = store.get("t1")
    assert task is not None
    assert task.status == TaskStatus.FAILED, "失败产出不得标成 idle"
    assert task.last_error, "last_error 为空——事后无法判断失败原因"

    store.close()


def test_search_failure_is_recognized(tmp_path: Path) -> None:
    """功能：检索不可用同样算失败（此前会被当作正常完成）。"""
    store = _make_store(tmp_path)
    store.save(_due_task())

    async def _runner(_task: object) -> str:
        return "search_unavailable: 搜索服务不可用（失败原因：mcp: HTTP 429）"

    _run_once(store, _runner)

    task = store.get("t1")
    assert task is not None and task.status == TaskStatus.FAILED
    assert "search_unavailable" in str(task.last_error)

    store.close()


def test_success_path_stays_idle_and_advances(tmp_path: Path) -> None:
    """功能：正常产出不得被误判为失败（防过度收紧）。"""
    store = _make_store(tmp_path)
    store.save(_due_task())

    async def _runner(_task: object) -> str:
        return "统计完成：.py 文件数 1234"

    _run_once(store, _runner)

    task = store.get("t1")
    assert task is not None
    assert task.status == TaskStatus.IDLE, f"正常产出被判失败：{task.last_error}"
    assert not task.last_error
    assert "1234" in str(task.last_output)

    store.close()


def test_silent_completion_is_not_a_failure(tmp_path: Path) -> None:
    """功能：空产出 / HEARTBEAT_OK 属「无事可报」，不是失败。"""
    store = _make_store(tmp_path)
    store.save(_due_task())

    async def _runner(_task: object) -> str:
        return "HEARTBEAT_OK"

    _run_once(store, _runner)

    task = store.get("t1")
    assert task is not None and task.status == TaskStatus.IDLE

    store.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("模型服务暂时不可用，本轮没有产出回复。", True),
        ("本轮执行失败：连接超时", True),
        ("LLM call failed (candidates disabled)", True),
        ("search_unavailable: 搜索服务不可用", True),
        ("统计完成：.py 文件数 1234", False),
        ("", False),
        ("HEARTBEAT_OK", False),
    ],
)
def test_failure_marker_matrix(text: str, expected: bool) -> None:
    """功能：失败判定矩阵——标志词命中即失败，正常产出不误判。"""
    assert (_failure_reason(text) is not None) is expected
