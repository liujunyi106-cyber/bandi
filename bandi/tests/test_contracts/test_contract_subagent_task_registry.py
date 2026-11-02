# 功能：契约——子代理并发护栏。
# 钉行为：tasks 上限 10 硬截断（丢弃数进 payload warning，非异常）；task_id 化与
# 回传（task_id/session_id 双轨）；cancel(task_id) 与模块级取消/查询（定向取消不
# 杀父流程，父取消仍传播）；完成快照 FIFO 200（成功/失败/超时/定向取消均落档）；
# max_concurrent 语义不变（既有契约保持）。
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.agent import subagent as sub_mod
from bandi.agent.subagent import (
    COMPLETED_SNAPSHOTS_CAP,
    SubagentConfig,
    SubagentManager,
    SubagentResult,
    cancel_active_task,
    clear_completed_tasks,
    get_completed_task,
    list_active_tasks,
    list_completed_tasks,
)
from bandi.agent.subagent_tool import MAX_SUBAGENT_TASKS, SubAgentTool
from bandi.core.config import ToolsConfig


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    # 功能：进程级注册表隔离——用例前后清零完成快照与活跃注册
    clear_completed_tasks()
    with sub_mod._ACTIVE_TASKS_LOCK:
        sub_mod._ACTIVE_TASKS.clear()
    yield
    clear_completed_tasks()
    with sub_mod._ACTIVE_TASKS_LOCK:
        sub_mod._ACTIVE_TASKS.clear()


def _mk_manager(
    monkeypatch: Any, busy_delay: float = 0.0
) -> tuple[SubagentManager, dict[str, Any]]:
    """_run_subagent 桩：任务名以 slow 开头则挂起 5s 可取消，其余延迟 busy_delay 后成功。"""
    manager = SubagentManager()
    manager._loop_checkpoint_store = None
    state: dict[str, Any] = {"started": [], "cancelled": [], "names": {}}

    async def fake_run(
        sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
    ):
        state["started"].append(session.task_id)
        state["names"][session.task_id] = task
        delay = 5.0 if task.startswith("slow") else busy_delay
        if delay:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                state["cancelled"].append(session.task_id)
                raise
        return SubagentResult(
            success=True,
            summary=f"done:{task}",
            message=f"msg:{task}",
            total_tokens=10,
            session_id=session.session_id,
            task_id=session.task_id,
        )

    monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", fake_run)
    return manager, state


class _SpyManager:
    seen: dict[str, Any] = {}

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def spawn_many(self, tasks, config, **kwargs):
        type(self).seen["tasks"] = list(tasks)
        type(self).seen["max_concurrent"] = kwargs.get("max_concurrent")
        return [
            SubagentResult(
                success=True,
                summary="ok",
                session_id=f"s{i}",
                task_id=f"subagent-{i:012x}",
            )
            for i, _ in enumerate(tasks)
        ]

    def cancel_all(self) -> None:
        pass

    def close(self) -> None:
        pass


def _run_tool(tool: SubAgentTool, params: dict[str, Any]) -> dict[str, Any]:
    from bandi.agent import subagent_tool as st_mod

    original = st_mod.SubagentManager
    st_mod.SubagentManager = _SpyManager  # type: ignore[assignment]
    try:
        return asyncio.run(tool.execute(params, None))
    finally:
        st_mod.SubagentManager = original  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────
# A. tasks 硬截断族
# ──────────────────────────────────────────────────────────────────────


class TestTaskTruncation:
    def test_tasks_truncated_to_limit_with_warning(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object())
        over = MAX_SUBAGENT_TASKS + 1
        result = _run_tool(tool, {"tasks": [f"t{i}" for i in range(over)]})
        assert _SpyManager.seen["tasks"] == [f"t{i}" for i in range(MAX_SUBAGENT_TASKS)], (
            f"只保留前 {MAX_SUBAGENT_TASKS} 个"
        )
        assert len(result["workers"]) == MAX_SUBAGENT_TASKS
        warning = result.get("warning", "")
        assert "1" in warning and "dropped" in warning, f"warning 必须报丢弃数: {warning!r}"

    def test_tasks_at_limit_not_truncated(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object())
        result = _run_tool(tool, {"tasks": [f"t{i}" for i in range(MAX_SUBAGENT_TASKS)]})
        assert len(result["workers"]) == MAX_SUBAGENT_TASKS
        assert "warning" not in result, "未超限不得出现 warning"

    def test_warning_counts_dropped(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object())
        extra = 7
        total = MAX_SUBAGENT_TASKS + extra
        result = _run_tool(tool, {"tasks": [f"t{i}" for i in range(total)]})
        assert str(extra) in result["warning"], (
            f"{total} 个应报丢弃 {extra} 个: {result['warning']!r}"
        )

    def test_truncation_preserves_max_concurrent(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object(), default_max_concurrency=3)
        over = MAX_SUBAGENT_TASKS + 2
        _run_tool(tool, {"tasks": [f"t{i}" for i in range(over)], "max_concurrent": 2})
        assert len(_SpyManager.seen["tasks"]) == MAX_SUBAGENT_TASKS, (
            "截断不得影响 max_concurrent 透传"
        )
        assert _SpyManager.seen["max_concurrent"] == 2, "LLM 显式 max_concurrent 原样透传"

    def test_single_task_mode_unaffected(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object())
        result = _run_tool(tool, {"task": "单个任务"})
        assert result["success"] is True
        assert _SpyManager.seen["tasks"] == ["单个任务"]
        assert "warning" not in result

    def test_truncation_limit_matches_concurrency_cap(self) -> None:
        # 否则并发上限 10 会被任务数上限卡住、放大动作不生效。
        assert MAX_SUBAGENT_TASKS == 10
        assert MAX_SUBAGENT_TASKS >= ToolsConfig().subagent_max_concurrency


# ──────────────────────────────────────────────────────────────────────
# B. task_id 族
# ──────────────────────────────────────────────────────────────────────


class TestTaskId:
    def test_worker_results_carry_unique_task_ids(self, monkeypatch: Any) -> None:
        manager, state = _mk_manager(monkeypatch)
        results = asyncio.run(manager.spawn_many(["w1", "w2", "w3"], SubagentConfig(timeout_sec=5)))
        ids = [r.task_id for r in results]
        assert all(ids), "每个 worker 必须回传 task_id"
        assert len(set(ids)) == 3, "task_id 必须唯一"
        assert set(ids) == set(state["started"]), "结果 task_id 与真实启动一致"

    def test_task_id_distinct_from_session_id(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)
        result = asyncio.run(manager.spawn("t", SubagentConfig(timeout_sec=5)))
        assert result.task_id and result.session_id
        assert result.task_id != result.session_id, "task_id 与 session_id 双轨互补"

    def test_payload_carries_task_id_multi(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object())
        result = _run_tool(tool, {"tasks": ["a", "b"]})
        assert [w["task_id"] for w in result["workers"]] == [
            "subagent-000000000000",
            "subagent-000000000001",
        ]

    def test_payload_carries_task_id_single(self) -> None:
        _SpyManager.seen.clear()
        tool = SubAgentTool(provider=object())
        result = _run_tool(tool, {"task": "a"})
        assert result["task_id"] == "subagent-000000000000"


# ──────────────────────────────────────────────────────────────────────
# C. cancel 族
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.timing
class TestCancel:
    """取消语义契约。

    `timing`：本类以 `asyncio.sleep` 调度窗口作断言（注册后 0.内须观察到
    3 个活跃任务，worker busy_delay=0.3s），隔离单跑 10-抖动明显；全量
    `-n 4` 下事件循环被抢占，窗口内未必完成注册 → 属负载敏感误报
    由 verify.ps1 独立低并发分组承载（取消语义本身仍被完整验证）。
    """

    def test_cancel_by_task_id_stops_single_worker(self, monkeypatch: Any) -> None:
        manager, state = _mk_manager(monkeypatch)

        async def scenario() -> SubagentResult:
            task = asyncio.create_task(manager.spawn("slow-task", SubagentConfig(timeout_sec=30)))
            await asyncio.sleep(0.1)
            active = list_active_tasks()
            assert len(active) == 1
            manager.cancel(active[0])
            return await task

        result = asyncio.run(scenario())
        assert result.success is False
        assert "cancel" in result.summary.lower(), f"取消结果语义: {result.summary!r}"
        assert state["cancelled"], "定向取消必须真实传达子任务"
        assert list_active_tasks() == [], "取消后注册表必须清空"

    def test_cancel_by_session_id_still_works(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)

        async def scenario() -> None:
            task = asyncio.create_task(manager.spawn("slow-old", SubagentConfig(timeout_sec=30)))
            await asyncio.sleep(0.1)
            active = manager.get_active()
            assert active
            session_id = next(iter(active))
            manager.cancel(session_id)
            await task

        asyncio.run(scenario())
        assert manager.get_active() == {}, "session_id 直传取消（兼容）必须仍生效"

    def test_cancel_one_worker_does_not_kill_parent(self, monkeypatch: Any) -> None:
        manager, state = _mk_manager(monkeypatch, busy_delay=0.3)

        async def scenario() -> list[SubagentResult]:
            gather = asyncio.create_task(
                manager.spawn_many(["slow-1", "w2", "w3"], SubagentConfig(timeout_sec=30))
            )
            await asyncio.sleep(0.05)
            active = list_active_tasks()
            assert len(active) == 3, "慢任务 + 两个忙碌兄弟都应在活跃面"
            slow_id = next(tid for tid, name in state["names"].items() if name.startswith("slow"))
            cancel_active_task(slow_id)
            return await gather

        results = asyncio.run(scenario())
        assert len(results) == 3, "定向取消不得炸掉聚合（父流程完好）"
        assert sum(1 for r in results if r.success) == 2, "兄弟 worker 必须完成"
        assert state["cancelled"], "被定向取消的必须恰是 slow worker"

    def test_outer_cancel_still_propagates(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)

        async def scenario() -> None:
            outer = asyncio.create_task(manager.spawn("slow-outer", SubagentConfig(timeout_sec=30)))
            await asyncio.sleep(0.1)
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer

        asyncio.run(scenario())

    def test_cancel_active_task_unknown_returns_false(self) -> None:
        assert cancel_active_task("subagent-missing") is False

    def test_manager_cancel_unknown_handle_no_crash(self) -> None:
        manager = SubagentManager()
        manager.cancel("subagent-missing")
        manager.cancel("session-missing")
        assert manager.get_active() == {}


# ──────────────────────────────────────────────────────────────────────
# D. 完成快照族（FIFO 200）
# ──────────────────────────────────────────────────────────────────────


class TestCompletedSnapshots:
    def test_snapshot_recorded_on_success(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)
        result = asyncio.run(manager.spawn("t", SubagentConfig(timeout_sec=5)))
        snapshot = get_completed_task(result.task_id)
        assert snapshot is not None
        assert snapshot.success is True
        assert snapshot.completed_at, "快照必须带完成时间戳"

    def test_snapshot_records_failure(self, monkeypatch: Any) -> None:
        manager = SubagentManager()
        manager._loop_checkpoint_store = None

        async def failing_run(
            sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
        ):
            return SubagentResult(
                success=False,
                summary="boom",
                session_id=session.session_id,
                task_id=session.task_id,
            )

        monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", failing_run)
        result = asyncio.run(manager.spawn("t", SubagentConfig(timeout_sec=5)))
        snapshot = get_completed_task(result.task_id)
        assert snapshot is not None and snapshot.success is False

    def test_snapshot_records_timeout(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)
        result = asyncio.run(manager.spawn("slow-timeout", SubagentConfig(timeout_sec=1)))
        assert result.success is False
        snapshot = get_completed_task(result.task_id)
        assert snapshot is not None, "超时任务也必须落档"
        assert "timed out" in snapshot.summary.lower()

    def test_snapshot_fifo_cap_200(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)

        async def scenario() -> list[str]:
            ids = []
            for i in range(COMPLETED_SNAPSHOTS_CAP + 3):
                r = await manager.spawn(f"t{i}", SubagentConfig(timeout_sec=5))
                ids.append(r.task_id)
            return ids

        ids = asyncio.run(scenario())
        snapshot = list_completed_tasks(limit=COMPLETED_SNAPSHOTS_CAP + 10)
        assert len(snapshot) == COMPLETED_SNAPSHOTS_CAP, "快照容量必须封顶 200"
        assert get_completed_task(ids[0]) is None, "最旧快照必须被 FIFO 淘汰"
        assert get_completed_task(ids[-1]) is not None, "最新快照必须保留"

    def test_list_completed_recent_first_and_limit(self, monkeypatch: Any) -> None:
        manager, _ = _mk_manager(monkeypatch)

        async def scenario() -> list[str]:
            ids = []
            for i in range(3):
                r = await manager.spawn(f"t{i}", SubagentConfig(timeout_sec=5))
                ids.append(r.task_id)
            return ids

        ids = asyncio.run(scenario())
        recent = list_completed_tasks(limit=2)
        assert [r.task_id for r in recent] == [ids[2], ids[1]], "最近完成优先"
        assert len(list_completed_tasks(limit=0)) == 0

    def test_e2e_mock_provider_snapshot_queryable(self) -> None:
        manager = SubagentManager()
        manager._loop_checkpoint_store = None
        result = asyncio.run(manager.spawn("真实链路探针", SubagentConfig(timeout_sec=10)))
        assert result.success
        snapshot = get_completed_task(result.task_id)
        assert snapshot is not None
        assert snapshot.message, "快照必须保留最终产出"
        assert list_active_tasks() == [], "完成后活跃面必须清空"


# ──────────────────────────────────────────────────────────────────────
# E. API 族
# ──────────────────────────────────────────────────────────────────────


class TestSubagentTasksApi:
    def _client(self, monkeypatch: Any) -> Any:
        import importlib

        from fastapi.testclient import TestClient

        # bandi.api 包属性 app 被 FastAPI 实例占用——必须经 sys.modules 取模块本体
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_API_KEY", "test")
        client = TestClient(app_mod.app)
        client.headers["Authorization"] = "Bearer test"
        return client

    def test_api_lists_active_and_completed(self, monkeypatch: Any) -> None:
        client = self._client(monkeypatch)
        body = client.get("/api/subagent/tasks").json()
        assert body == {"active": [], "completed": []}

        sub_mod.record_completed_task(
            SubagentResult(success=True, summary="s", task_id="subagent-abc", session_id="sess")
        )
        body = client.get("/api/subagent/tasks").json()
        assert [t["task_id"] for t in body["completed"]] == ["subagent-abc"]
        entry = body["completed"][0]
        assert entry["success"] is True and entry["summary"] == "s"

    def test_api_cancel_unknown_404(self, monkeypatch: Any) -> None:
        client = self._client(monkeypatch)
        resp = client.post("/api/subagent/tasks/subagent-missing/cancel")
        assert resp.status_code == 404

    def test_api_cancel_real_active_task_200(self, monkeypatch: Any) -> None:
        # 跨 loop 真实取消：bg 线程跑 asyncio loop 的挂起任务，TestClient（另一 loop）
        # 经 call_soon_threadsafe 取消——同时验证 cancel_active_task 真实命中语义
        import threading
        import time

        from bandi.agent import subagent as sub_mod
        from bandi.agent.subagent import SubagentConfig, SubagentManager

        cancelled = threading.Event()

        async def fake_run(
            sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
        ):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", fake_run)

        def bg() -> None:
            async def scenario() -> None:
                manager = SubagentManager()
                manager._loop_checkpoint_store = None
                await manager.spawn("slow", SubagentConfig(timeout_sec=60))

            asyncio.run(scenario())

        thread = threading.Thread(target=bg, daemon=True)
        thread.start()
        deadline = time.time() + 8
        active: list[str] = []
        while time.time() < deadline:
            active = list_active_tasks()
            if active:
                break
            time.sleep(0.02)
        assert active, "后台挂起任务必须注册活跃 task_id"

        client = self._client(monkeypatch)
        resp = client.post(f"/api/subagent/tasks/{active[0]}/cancel")
        assert resp.status_code == 200, resp.text
        assert cancelled.wait(5), "跨 loop 取消必须真实传达（call_soon_threadsafe）"
        thread.join(5)
        assert client.post("/api/subagent/tasks/subagent-missing/cancel").status_code == 404

    def test_api_active_entry_shape(self, monkeypatch: Any) -> None:
        client = self._client(monkeypatch)
        manager = SubagentManager()
        with sub_mod._ACTIVE_TASKS_LOCK:
            sub_mod._ACTIVE_TASKS["subagent-live"] = manager
        try:
            body = client.get("/api/subagent/tasks").json()
            assert body["active"] == ["subagent-live"]
        finally:
            with sub_mod._ACTIVE_TASKS_LOCK:
                sub_mod._ACTIVE_TASKS.pop("subagent-live", None)
