# 功能：契约——/中断链路生产装配（abort_controller 从入口透传到工具执行面）。
# 钉行为：run_agent_turn/build_agent_context 装配的 ToolExecutor 必须持有入口
# 竞速取消后必须等待被取消工具清理完成（进程树击杀等）才返回；
# SubAgentTool 声明可取消——父中断时经 CancelledError 分支清理全部 worker。
from __future__ import annotations

import asyncio
from typing import Any

from bandi.core.abort_controller import AbortController
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class _SlowCancellableTool(BaseTool):
    name = "slow_cancellable"
    description = "可取消慢工具"
    category = ToolCategory.UTIL
    cancellable = True
    cleanup_done = False

    async def execute(self, params, context=None) -> str:  # type: ignore[override]
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # 清理本身也是异步的（真实场景：杀进程树/回收 Job）——竞速返回必须等它完成
            await asyncio.sleep(0.05)
            type(self).cleanup_done = True
            raise
        return "late"


# （装配级）：build_agent_context 显式 abort_controller → executor 必须持有它
def test_build_agent_context_wires_abort_controller(tmp_path) -> None:
    from bandi.agent.factory import build_agent_context
    from bandi.llm.mock import MockProvider

    controller = AbortController()
    ctx = build_agent_context(
        user_input="hi",
        provider=MockProvider(),
        session_id="s-abort-wire",
        abort_controller=controller,
        project_root=str(tmp_path),
    )

    assert ctx.tool_executor is not None
    assert ctx.tool_executor._abort_controller is controller, (
        "装配链必须把 AbortController 透传进 ToolExecutor（工具级取消通道）"
    )


# （行为级）：cancellable 工具执行中 abort → 快速取消且清理完成才返回
def test_cancellable_tool_abort_waits_for_cleanup() -> None:
    _SlowCancellableTool.cleanup_done = False
    controller = AbortController()
    registry = ToolRegistry()
    registry.register_instance(_SlowCancellableTool())
    executor = ToolExecutor(registry=registry, abort_controller=controller)

    async def _run() -> tuple[Any, float]:
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, lambda: controller.abort("user_interrupt"))
        started = loop.time()
        result = await executor.execute([ToolCall(id="c1", name="slow_cancellable", arguments={})])
        return result[0], loop.time() - started

    result, elapsed = asyncio.run(_run())

    assert result.success is False and "abort" in (result.error or "").lower()
    assert elapsed < 5, "中断必须在竞速点即时生效（不得等工具自然结束）"
    assert _SlowCancellableTool.cleanup_done is True, "取消返回前必须等待工具清理完成"


# （子代理声明面 + 行为）：SubAgentTool 可取消——父中断经取消分支清理 worker
def test_subagent_tool_is_cancellable_and_cleans_up(monkeypatch) -> None:
    from bandi.agent import subagent_tool as st_mod
    from bandi.agent.subagent_tool import SubAgentTool

    assert SubAgentTool.cancellable is True, (
        "子代理工具必须声明可取消（否则父中断挂到全部 worker 结束）"
    )

    spawned = asyncio.Event()
    cancel_all_called = {"value": False}

    class _FakeManager:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def spawn_many(self, tasks, config, **kwargs):
            spawned.set()
            await asyncio.sleep(30)
            return []

        def cancel_all(self) -> None:
            cancel_all_called["value"] = True

        def close(self) -> None:
            pass

    monkeypatch.setattr(st_mod, "SubagentManager", _FakeManager)
    controller = AbortController()
    registry = ToolRegistry()
    registry.register_instance(SubAgentTool(provider=object()))
    executor = ToolExecutor(registry=registry, abort_controller=controller)

    async def _run() -> Any:
        loop = asyncio.get_running_loop()
        task = asyncio.ensure_future(
            executor.execute(
                [ToolCall(id="c1", name="subagent_spawn", arguments={"tasks": ["a", "b"]})]
            )
        )
        await spawned.wait()
        loop.call_later(0.1, lambda: controller.abort("user_interrupt"))
        return (await task)[0]

    result = asyncio.run(_run())

    assert result.success is False
    assert cancel_all_called["value"] is True, "父中断必须触发子代理清理（cancel_all）"


# （全链装配）：run_agent_turn → factory 的 ToolExecutor 构造收到同一 controller
def test_run_agent_turn_passes_controller_to_executor(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    from bandi.agent import factory as factory_mod
    from bandi.core import agent_runner as runner_mod
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    cfg = factory_mod.BandiConfig()
    cfg.llm.api_key = ""
    monkeypatch.setattr(factory_mod.BandiConfig, "load", staticmethod(lambda: cfg))

    seen: list[Any] = []
    real_executor_cls = factory_mod.ToolExecutor

    class _SpyExecutor(real_executor_cls):  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.append(kwargs.get("abort_controller"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(factory_mod, "ToolExecutor", _SpyExecutor)
    monkeypatch.setattr(runner_mod, "build_provider", lambda *_a, **_k: MockProvider())

    store = SessionStore(str(tmp_path / "s.db"))
    controller = AbortController()
    try:
        asyncio.run(
            runner_mod.run_agent_turn(
                user_input="你好",
                session_id="s-runner-abort",
                session_store=store,
                abort_controller=controller,
                bandi_config=cfg,
            )
        )
    finally:
        store.close()

    assert controller in seen, (
        f"run_agent_turn 必须把 AbortController 传给 ToolExecutor（实际 {seen}）"
    )
