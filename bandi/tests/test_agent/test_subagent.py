import pytest

from bandi.agent.subagent import SubagentConfig, SubagentManager
from bandi.agent.subagent_tool import SubAgentTool


@pytest.mark.asyncio
class TestSubagentManager:
    async def test_spawn_and_result(self) -> None:
        manager = SubagentManager()
        result = await manager.spawn("Analyze this data")
        assert result.success
        assert "Analyze" in result.summary

    async def test_active_tracking(self) -> None:
        manager = SubagentManager()
        assert len(manager.get_active()) == 0
        result = await manager.spawn("task")
        assert result.success
        assert len(manager.get_active()) == 0

    async def test_custom_config(self) -> None:
        manager = SubagentManager()
        config = SubagentConfig(max_turns=5, timeout_sec=30)
        result = await manager.spawn("Write a test", config)
        assert result.success
        assert "Write a test" in result.summary


@pytest.mark.asyncio
class TestSubAgentCancel:
    async def test_cancel_really_stops_task(self) -> None:
        import asyncio

        from bandi.llm.base import BaseProvider

        calls = {"n": 0}

        from bandi.core.message import LLMResponse, TokenUsage

        class SlowProvider(BaseProvider):
            def __init__(self) -> None:
                super().__init__(config=None)
                self._supports_streaming = False

            @property
            def supports_streaming(self) -> bool:
                return self._supports_streaming

            @property
            def model_name(self) -> str:
                return "slow"

            @property
            def protocol(self) -> str:
                return "openai"

            async def complete(self, messages, config=None, **kwargs):
                calls["n"] += 1
                await asyncio.sleep(10)
                return LLMResponse(
                    content="x",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
                    finish_reason="stop",
                )

            async def stream(self, messages, config=None, **kwargs):
                yield LLMResponse(
                    content="x",
                    tool_calls=[],
                    usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
                    finish_reason="stop",
                )

        manager = SubagentManager(provider=SlowProvider())
        spawn_task = asyncio.create_task(manager.spawn("long task"))
        await asyncio.sleep(0.3)
        active = manager.get_active()
        assert active, "任务应处于活跃状态"
        sid = next(iter(active))
        manager.cancel(sid)
        await asyncio.sleep(0.3)
        assert manager.get_active() == {}, "cancel 后不得残留会话"
        assert calls["n"] <= 1, f"取消后任务不得继续执行到完成，complete 调用 {calls['n']} 次"
        spawn_task.cancel()

    async def test_cancel_unknown_session_no_crash(self) -> None:
        manager = SubagentManager()
        manager.cancel("missing-id")
        assert manager.get_active() == {}


@pytest.mark.asyncio
class TestSubAgentToolProvider:
    async def test_provider_injected_into_manager(self) -> None:
        sentinel = object()
        tool = SubAgentTool(provider=sentinel)  # type: ignore[arg-type]
        from unittest.mock import patch

        with patch("bandi.agent.subagent_tool.SubagentManager") as mock_cls:
            mock_cls.return_value.spawn_many.return_value = None  # type: ignore[attr-defined]
            mock_cls.return_value.spawn_many.side_effect = (  # type: ignore[attr-defined]
                RuntimeError("stopped")
            )
            try:
                await tool.execute({"task": "t"})
            except RuntimeError:
                pass
        args, kwargs = mock_cls.call_args
        assert kwargs.get("provider") is sentinel or (args and args[0] is sentinel), (
            f"provider 必须注入 manager，实际 args={args} kwargs={kwargs}"
        )

    async def test_verified_is_success(self, monkeypatch) -> None:
        from bandi.agent.base import TerminalReason

        async def fake_run(self, ctx, session_timeout_sec=None):
            return TerminalReason.VERIFIED

        monkeypatch.setattr("bandi.agent.loop.AgentLoop.run", fake_run)
        manager = SubagentManager()
        result = await manager.spawn("Verify this", SubagentConfig(timeout_sec=30))
        assert result.success is True
