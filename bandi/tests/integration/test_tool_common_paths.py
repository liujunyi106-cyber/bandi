from __future__ import annotations

from pathlib import Path

import pytest

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.agent.loop import AgentLoop
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole, ToolCall
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from tests.integration.helpers import MockProvider, RecordingBus


class GlobTool(BaseTool):
    name = "glob"
    is_concurrency_safe = True

    async def execute(self, params: dict, context: object | None = None) -> str:
        from bandi.tools.glob_tool import GlobTool as _GlobTool

        return await _GlobTool().execute(params)


class GrepTool(BaseTool):
    name = "grep"
    is_concurrency_safe = True

    async def execute(self, params: dict, context: object | None = None) -> str:
        from bandi.tools.grep_tool import GrepTool as _GrepTool

        return await _GrepTool().execute(params)


class EditTool(BaseTool):
    name = "edit"
    is_concurrency_safe = False

    async def execute(self, params: dict, context: object | None = None) -> str:
        from bandi.tools.file_edit import FileEditTool as _EditTool

        return await _EditTool().execute(params)


@pytest.fixture
def tmp_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    (tmp_path / "hello.py").write_text("def greet():\n    return 'hello'")
    (tmp_path / "main.py").write_text("print('hello world')")
    return str(tmp_path)


class TestToolCommonPathsIntegration:
    @pytest.mark.asyncio
    async def test_glob_in_agent_loop(self, tmp_workspace: str) -> None:
        registry = ToolRegistry()
        registry.register_instance(GlobTool())
        executor = ToolExecutor(registry=registry)
        bus = RecordingBus()
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1", name="glob", arguments={"pattern": "*.py", "path": tmp_workspace}
                    )
                ],
            )
        )
        provider.add_response(LLMResponse(content="found files"))
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="list files")],
            provider=provider,
            config=AgentConfig(max_iterations=3),
            bus=bus,
        )
        ctx.tool_executor = executor
        ctx.active_tools = registry.list_schemas()
        reason = await AgentLoop(max_iterations=3).run(ctx)
        assert reason == TerminalReason.COMPLETED
        tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
        assert len(tool_msgs) >= 1
        assert "hello.py" in (tool_msgs[0].content or "")

    @pytest.mark.asyncio
    async def test_grep_in_agent_loop(self, tmp_workspace: str) -> None:
        registry = ToolRegistry()
        registry.register_instance(GrepTool())
        executor = ToolExecutor(registry=registry)
        bus = RecordingBus()
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1", name="grep", arguments={"pattern": "hello", "path": tmp_workspace}
                    )
                ],
            )
        )
        provider.add_response(LLMResponse(content="found matches"))
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="search hello")],
            provider=provider,
            config=AgentConfig(max_iterations=3),
            bus=bus,
        )
        ctx.tool_executor = executor
        ctx.active_tools = registry.list_schemas()
        reason = await AgentLoop(max_iterations=3).run(ctx)
        assert reason == TerminalReason.COMPLETED
        tool_msgs = [m for m in ctx.messages if m.role == MessageRole.TOOL]
        assert len(tool_msgs) >= 1
        assert "return" in (tool_msgs[0].content or "")

    @pytest.mark.asyncio
    async def test_edit_in_agent_loop(
        self, tmp_workspace: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(Path(tmp_workspace)))
        registry = ToolRegistry()
        registry.register_instance(EditTool())
        executor = ToolExecutor(registry=registry)
        bus = RecordingBus()
        provider = MockProvider()
        fp = Path(tmp_workspace) / "main.py"
        fp.write_text("hello old")
        provider.add_response(
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="edit",
                        arguments={
                            "file_path": "main.py",
                            "old_string": "hello old",
                            "new_string": "hello new",
                        },
                    )
                ],
            )
        )
        provider.add_response(LLMResponse(content="file edited"))
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="edit file")],
            provider=provider,
            config=AgentConfig(max_iterations=3),
            bus=bus,
        )
        ctx.tool_executor = executor
        ctx.active_tools = registry.list_schemas()
        reason = await AgentLoop(max_iterations=3).run(ctx)
        assert reason == TerminalReason.COMPLETED
        assert "hello new" in fp.read_text("utf-8")
