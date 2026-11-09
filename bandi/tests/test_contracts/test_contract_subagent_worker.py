# 功能：AgentTool 对齐增强契约——worker 并行聚合 / 并发上限 / 独立会话回溯

from __future__ import annotations

import asyncio
from typing import Any

from bandi.agent.subagent import SubagentConfig, SubagentManager, SubagentResult


def _mk_manager(monkeypatch: Any) -> SubagentManager:
    """MockProvider 驱动：_run_subagent 替换为可记录并发的桩。"""
    import bandi.agent.subagent as sub_mod

    manager = SubagentManager()
    manager._loop_checkpoint_store = None  # 契约不落盘，避免 sqlite
    state: dict[str, Any] = {"inflight": 0, "peak": 0, "spawned": []}

    async def fake_run(
        sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
    ):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        state["spawned"].append(session.session_id)
        await asyncio.sleep(0.02)
        state["inflight"] -= 1
        return SubagentResult(
            success=True,
            summary=f"done:{task}",
            message=f"msg:{task}",
            total_tokens=10,
            session_id=session.session_id,
        )

    monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", fake_run)
    monkeypatch.setattr(
        sub_mod.SubagentManager, "spawn", sub_mod.SubagentManager.spawn
    )  # 保真实 spawn 调度
    return manager, state


async def _spawn_many(
    manager: SubagentManager, tasks: list[str], max_concurrent: int = 0
) -> list[SubagentResult]:
    return await manager.spawn_many(
        tasks, SubagentConfig(max_turns=1, timeout_sec=10), max_concurrent=max_concurrent
    )


def test_worker_results_aggregated_in_order(monkeypatch: Any) -> None:
    """多 worker 结果聚合：数量/顺序/内容与任务列表一一对应。"""
    manager, state = _mk_manager(monkeypatch)
    tasks = ["w1", "w2", "w3"]
    results = asyncio.run(_spawn_many(manager, tasks))
    assert [r.summary for r in results] == ["done:w1", "done:w2", "done:w3"]
    assert all(r.success for r in results)
    assert len(state["spawned"]) == 3


def test_max_concurrent_limits_parallelism(monkeypatch: Any) -> None:
    """并发上限：max_concurrent=2 限制同时运行 worker ≤2。"""
    manager, state = _mk_manager(monkeypatch)
    tasks = ["w1", "w2", "w3", "w4", "w5"]
    results = asyncio.run(_spawn_many(manager, tasks, max_concurrent=2))
    assert state["peak"] <= 2, f"peak={state['peak']} 必须 ≤ max_concurrent=2"
    assert len(results) == 5, "限流是排队而非丢弃"


def test_default_concurrent_is_full_parallel(monkeypatch: Any) -> None:
    """默认全并行：max_concurrent=0 → peak=worker 数。"""
    manager, state = _mk_manager(monkeypatch)
    tasks = ["w1", "w2", "w3"]
    asyncio.run(_spawn_many(manager, tasks))
    assert state["peak"] == 3, f"全并行时必须 peak=3, 实际 {state['peak']}"


def test_each_worker_has_independent_session(monkeypatch: Any) -> None:
    """独立会话回溯：每个 worker session_id 唯一且回传结果。"""
    manager, state = _mk_manager(monkeypatch)
    tasks = ["w1", "w2", "w3"]
    results = asyncio.run(_spawn_many(manager, tasks))
    ids = [r.session_id for r in results]
    assert ids and len(set(ids)) == 3, "每个 worker 必须独立 session_id"
    assert set(ids) == set(state["spawned"]), "结果 session_id 与真实启动会话一致"


def test_worker_failure_isolated(monkeypatch: Any) -> None:
    """worker 失败隔离：一个失败不阻断兄弟 worker 完成。"""
    import bandi.agent.subagent as sub_mod

    manager = SubagentManager()
    manager._loop_checkpoint_store = None

    calls: list[str] = []

    async def fake_run(
        sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
    ):
        calls.append(task)
        if task == "w2":
            return SubagentResult(success=False, summary="boom", session_id=session.session_id)
        return SubagentResult(success=True, summary=f"ok:{task}", session_id=session.session_id)

    monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", fake_run)
    tasks = ["w1", "w2", "w3"]
    results = asyncio.run(_spawn_many(manager, tasks))
    assert [r.success for r in results] == [True, False, True]
    assert set(calls) == {"w1", "w2", "w3"}, "三个 worker 都必须被派生"


def test_empty_tasks_returns_empty(monkeypatch: Any) -> None:
    """空任务列表：返回空聚合，不报错。"""
    manager, _ = _mk_manager(monkeypatch)
    results = asyncio.run(_spawn_many(manager, []))
    assert results == []


def test_single_task_backward_compatible(monkeypatch: Any) -> None:
    """单任务 spawn 保持既有签名：spawn(task) 返回 SubagentResult 不含 worker 聚合。"""
    import bandi.agent.subagent as sub_mod

    manager = SubagentManager()
    manager._loop_checkpoint_store = None

    async def fake_run(
        sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
    ):
        return SubagentResult(success=True, summary="singleton", session_id=session.session_id)

    monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", fake_run)
    result = asyncio.run(manager.spawn("t", SubagentConfig(max_turns=1, timeout_sec=10)))
    assert result.success is True
    assert result.summary == "singleton"


def test_schema_accepts_tasks_and_task_but_rejects_both_missing() -> None:
    """承接生产 executor 链路：subagent_spawn 的 validate_params 必须放行 task 与 tasks 双模式。"""
    from bandi.agent.subagent_tool import SubAgentTool

    tool = SubAgentTool()
    assert tool.validate_params({"task": "单任务"}) == []
    assert tool.validate_params({"tasks": ["w1", "w2"], "max_concurrent": 2}) == []
    errors = tool.validate_params({})
    assert errors, "task 与 tasks 都缺时必须校验失败（生产 LLM 请求不会带空调用的成本）"


def test_tool_forwards_max_concurrent_to_manager(monkeypatch: Any) -> None:
    """SubAgentTool execute 将 LLM 参数 max_concurrent/tasks 传透 spawn_many（接线完整）。"""
    import bandi.agent.subagent as sub_mod

    captured: dict[str, Any] = {}

    async def fake_spawn_many(
        sub_self: Any,
        tasks,
        config,
        tool_registry=None,
        parent_trace_id=None,
        depth=0,
        max_concurrent=0,
    ):
        captured["tasks"] = tasks
        captured["max_concurrent"] = max_concurrent
        captured["depth"] = depth
        return [
            SubagentResult(success=True, summary="ok", session_id="s1"),
            SubagentResult(success=True, summary="ok", session_id="s2"),
        ]

    monkeypatch.setattr(sub_mod.SubagentManager, "spawn_many", fake_spawn_many)
    from bandi.agent.subagent_tool import SubAgentTool

    tool = SubAgentTool()
    result = asyncio.run(tool.execute({"tasks": ["t1", "t2"], "max_concurrent": 2}))
    assert len(result["workers"]) == 2
    assert captured["tasks"] == ["t1", "t2"]
    assert captured["max_concurrent"] == 2
    assert captured["depth"] == 1, "多 worker 模式也要深度继承"
    assert "session_id" in result["workers"][0], "多 worker 聚合逐 worker 回传 session_id"


def test_worker_real_exception_isolated_from_siblings(monkeypatch: Any) -> None:
    """审查修复：worker 抛真异常（非 SubagentResult 失败）不得传播阻断兄弟——
    降级为 success=False 结果，兄弟照常完成（gather 不炸）。"""
    import bandi.agent.subagent as sub_mod

    manager = SubagentManager()
    manager._loop_checkpoint_store = None
    crashed = {"boom": False}

    async def crashy_run(
        sub_self: Any, task, cfg, session, tool_registry=None, parent_trace_id="", depth=0
    ):
        if task.startswith("crash"):
            crashed["boom"] = True
            raise RuntimeError("worker exploded")
        await asyncio.sleep(0.01)
        return SubagentResult(success=True, summary=f"done:{task}", session_id=session.session_id)

    monkeypatch.setattr(sub_mod.SubagentManager, "_run_subagent", crashy_run)
    monkeypatch.setattr(sub_mod.SubagentManager, "spawn", sub_mod.SubagentManager.spawn)

    results = asyncio.run(
        manager.spawn_many(["crash-1", "ok-1", "crash-2", "ok-2"], max_concurrent=4)
    )
    assert crashed["boom"] is True, "crash worker 确实抛了真异常"
    assert len(results) == 4, "gather 不得中断——4 worker 结果齐全"
    assert [r.success for r in results] == [
        False,
        True,
        False,
        True,
    ], "crash worker 降级 success=False，兄弟保持 success=True"
    assert results[0].summary and "crash" not in results[1].summary
