from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from bandi.core.event_bus import EventBus
from bandi.core.events import EventType
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolDependency
from bandi.tools.bash import BashTool
from bandi.tools.dependency_checker import DependencyChecker
from bandi.tools.executor import ToolExecutor
from bandi.tools.file_ops import FileReadTool, FileWriteTool
from bandi.tools.registry import ToolRegistry
from bandi.tools.web_search import WebSearchTool

_WORKSPACE_ROOT = Path.home() / ".bandi" / "workspace"


@pytest.fixture(autouse=True)
def _sandboxed_workspace(tmp_path, monkeypatch):
    import tests.test_tools.test_tools as tt_mod

    monkeypatch.setattr(tt_mod, "_WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)
    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.fixture
def executor(registry: ToolRegistry) -> ToolExecutor:
    return ToolExecutor(registry, max_output_tokens=8, temp_dir=tempfile.mkdtemp())


@pytest.fixture
def tmp_file() -> str:
    _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    path = _WORKSPACE_ROOT / "tmp_test.txt"
    path.write_text("hello world\nline 2\nline 3\n")
    return str(path)


# ── BaseTool ──────────────────────────────────────────────────────────


class EchoTool(BaseTool):
    name: str = "echo"
    description: str = "echoes input"
    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"msg": {"type": "string"}},
    }

    async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
        return params.get("msg", "")


class TestBaseTool:
    def test_to_openai_schema(self) -> None:
        tool = EchoTool()
        schema = tool.to_openai_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "echo"
        assert "parameters" in schema["function"]

    def test_defaults(self) -> None:
        tool = EchoTool()
        assert tool.enabled
        assert not tool.is_concurrency_safe
        assert tool.dependencies == []

    def test_validate_params_valid(self) -> None:
        tool = EchoTool()
        errors = tool.validate_params({"msg": "hello"})
        assert errors == []

    def test_validate_params_missing_required(self) -> None:
        class ReqTool(BaseTool):
            name: str = "req"
            description: str = "required test"
            parameters: dict = {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }

            async def execute(self, params: dict, **kwargs: Any) -> str:
                return "ok"

        tool = ReqTool()
        errors = tool.validate_params({})
        assert any("name" in e for e in errors)

    def test_validate_params_wrong_type(self) -> None:
        tool = EchoTool()
        errors = tool.validate_params({"msg": 123})
        assert any("string" in e for e in errors)

    def test_validate_params_unknown_field(self) -> None:
        tool = EchoTool()
        errors = tool.validate_params({"msg": "hello", "extra": "bad"})
        assert any("未知" in e or "extra" in e for e in errors)

    def test_validate_params_empty_schema(self) -> None:
        class NoParamTool(BaseTool):
            name: str = "noparam"
            description: str = "no params"
            parameters: dict = {}

            async def execute(self, params: dict, **kwargs: Any) -> str:
                return "ok"

        tool = NoParamTool()
        assert tool.validate_params({"anything": 1}) == []


# ── DependencyChecker ────────────────────────────────────────────────


class TestDependencyChecker:
    def test_check_binary_exists(self) -> None:
        checker = DependencyChecker()
        py_name = os.path.basename(sys.executable)
        dep = ToolDependency(type="binary", name=py_name, help_hint="need python")
        result = checker.check(dep)
        assert result is True

    def test_check_binary_missing(self) -> None:
        checker = DependencyChecker()
        dep = ToolDependency(type="binary", name="nonexistent_cmd_xyz", help_hint="install it")
        result = checker.check(dep)
        assert result is False

    def test_check_python_package_exists(self) -> None:
        checker = DependencyChecker()
        dep = ToolDependency(type="python_package", name="os", help_hint="stdlib")
        result = checker.check(dep)
        assert result is True

    def test_describe_missing(self) -> None:
        checker = DependencyChecker()
        py_name = os.path.basename(sys.executable)
        deps = [
            ToolDependency(type="binary", name=py_name, help_hint="need python"),
            ToolDependency(type="binary", name="nonexistent_xyz", help_hint="install xyz"),
        ]
        missing = checker.describe_missing(deps)
        assert len(missing) == 1
        assert "nonexistent_xyz" in missing[0]


# ── ToolRegistry ──────────────────────────────────────────────────────


class TestToolRegistry:
    def test_register_and_get(self, registry: ToolRegistry) -> None:
        registry.register(EchoTool)
        tool = registry.get("echo")
        assert tool is not None
        assert tool.name == "echo"

    def test_get_unknown(self, registry: ToolRegistry) -> None:
        tool = registry.get("nonexistent")
        assert tool is None

    def test_list_schemas(self, registry: ToolRegistry) -> None:
        registry.register(EchoTool)
        schemas = registry.list_schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "echo"

    def test_register_instance(self, registry: ToolRegistry) -> None:
        tool = EchoTool()
        registry.register_instance(tool)
        assert registry.get("echo") is tool


# ── ToolExecutor ──────────────────────────────────────────────────────


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_empty_calls(self, executor: ToolExecutor) -> None:
        results = await executor.execute([])
        assert results == []

    @pytest.mark.asyncio
    async def test_unknown_tool(self, executor: ToolExecutor) -> None:
        calls = [ToolCall(id="1", name="nonexistent", arguments={})]
        results = await executor.execute(calls)
        assert len(results) == 1
        assert not results[0].success
        assert "unknown" in results[0].error

    @pytest.mark.asyncio
    async def test_output_success(self, executor: ToolExecutor) -> None:
        executor._registry.register(EchoTool)
        calls = [ToolCall(id="1", name="echo", arguments={"msg": "hi"})]
        results = await executor.execute(calls)
        assert len(results) == 1
        assert results[0].success
        assert results[0].output == "hi"

    def test_partition_concurrent_safe(self, executor: ToolExecutor) -> None:
        registry = executor._registry
        registry.register(EchoTool)

        class SafeTool(BaseTool):
            name: str = "safe"
            description: str = ""
            is_concurrency_safe: bool = True

            async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
                return "ok"

        registry.register_instance(SafeTool())

        calls = [
            ToolCall(id="1", name="echo", arguments={}),
            ToolCall(id="2", name="safe", arguments={}),
        ]
        concurrent, serial = executor._partition(calls)
        assert len(concurrent) == 1
        assert concurrent[0].name == "safe"
        assert len(serial) == 1
        assert serial[0].name == "echo"

    @pytest.mark.asyncio
    async def test_output_truncation_p0_6(self, executor: ToolExecutor) -> None:
        executor._registry.register(EchoTool)
        long_msg = "x" * (executor._max_output_chars + 1000)
        calls = [ToolCall(id="1", name="echo", arguments={"msg": long_msg})]
        results = await executor.execute(calls)
        assert len(results) == 1
        assert results[0].success
        # 小帽 fixture 下摘要（≤1200）+ 回传行仍有界
        assert "[tool:summary] tool=echo" in results[0].output
        assert "[full output persisted:" in results[0].output
        assert len(results[0].output) < 2000

    @pytest.mark.asyncio
    async def test_persist_full_output(self, executor: ToolExecutor) -> None:
        executor._registry.register(EchoTool)
        long_msg = "y" * (executor._max_output_chars + 500)
        calls = [ToolCall(id="1", name="echo", arguments={"msg": long_msg})]
        results = await executor.execute(calls)
        assert results[0].success
        persist_dir = Path(executor._temp_dir)
        saved_files = list(persist_dir.glob("tool_output_echo_*.txt"))
        assert len(saved_files) >= 1


# ── BashTool ─────────────────────────────────────────────────────────


class TestBashTool:
    @pytest.mark.skipif(
        os.name == "nt" and not os.environ.get("CI"),
        reason="bash not available on Windows without WSL",
    )
    @pytest.mark.asyncio
    async def test_echo(self) -> None:
        tool = BashTool()
        result = await tool.execute({"command": "echo hello", "timeout": 5})
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        tool = BashTool()

        class _HangingStream:
            async def read(self, n: int = -1) -> bytes:
                await _asyncio.Event().wait()
                return b""

        mock_proc = MagicMock()
        mock_proc.stdout = _HangingStream()
        mock_proc.stderr = _HangingStream()
        mock_proc.pid = 4321
        mock_proc.wait = AsyncMock()

        with (
            patch("bandi.tools.bash.asyncio.create_subprocess_exec", return_value=mock_proc),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            await tool.execute({"command": "echo hello", "timeout": 1})

    def test_resolve_shell(self) -> None:
        tool = BashTool()
        shell = tool._resolve_shell()
        assert shell is not None
        assert os.path.isabs(shell)


# ── FileReadTool ──────────────────────────────────────────────────────


class TestFileReadTool:
    @pytest.mark.asyncio
    async def test_read_file(self, tmp_file: str) -> None:
        tool = FileReadTool()
        result = await tool.execute({"path": tmp_file})
        assert "hello world" in result
        assert "line 3" in result

    @pytest.mark.asyncio
    async def test_read_with_offset(self, tmp_file: str) -> None:
        tool = FileReadTool()
        result = await tool.execute({"path": tmp_file, "offset": 2})
        assert "hello world" not in result
        assert "line 2" in result

    @pytest.mark.asyncio
    async def test_read_with_limit(self, tmp_file: str) -> None:
        tool = FileReadTool()
        result = await tool.execute({"path": tmp_file, "offset": 1, "limit": 1})
        assert "1|hello world" in result
        assert "line 2" not in result

    @pytest.mark.asyncio
    async def test_file_not_found(self) -> None:
        tool = FileReadTool()
        with pytest.raises(FileNotFoundError):
            await tool.execute({"path": str(_WORKSPACE_ROOT / "nonexistent_file.txt")})


# ── FileWriteTool ─────────────────────────────────────────────────────


class TestFileWriteTool:
    @pytest.mark.asyncio
    async def test_write_new_file(self) -> None:
        _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        path = _WORKSPACE_ROOT / "new.txt"
        path.unlink(missing_ok=True)
        tool = FileWriteTool()
        result = await tool.execute({"path": str(path), "content": "test content"})
        assert "written" in result
        assert path.read_text() == "test content"

    @pytest.mark.asyncio
    async def test_write_mode_append(self) -> None:
        _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        path = _WORKSPACE_ROOT / "append.txt"
        path.write_text("base ")
        tool = FileWriteTool()
        await tool.execute({"path": str(path), "content": "appended", "mode": "append"})
        assert path.read_text() == "base appended"

    @pytest.mark.asyncio
    async def test_write_overwrite(self) -> None:
        _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        path = _WORKSPACE_ROOT / "overwrite.txt"
        path.write_text("old")
        tool = FileWriteTool()
        await tool.execute({"path": str(path), "content": "new", "mode": "write"})
        assert path.read_text() == "new"


# ── WebSearchTool ────────────────────────────────────────────────────


class FailTool(BaseTool):
    name: str = "failer"
    description: str = "always fails"

    async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
        msg = params.get("msg", "fail")
        raise RuntimeError(msg)


class TestWebSearchTool:
    @pytest.mark.asyncio
    @pytest.mark.heavy
    async def test_empty_query(self) -> None:
        tool = WebSearchTool()
        result = await tool.execute({"query": ""})
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_parse_ddg_empty_html(self) -> None:
        tool = WebSearchTool()
        results = tool._parse_ddg_html("<html></html>", 5)
        assert results == []

    @pytest.mark.asyncio
    async def test_parse_ddg_realistic_html(self) -> None:
        html = """<html><body>
<div><a href="https://example.com" class="result-link">Example Title</a></div>
<div class="snippet">Example snippet text</div>
<a href="https://test.org" class="result-link">Test Org</a>
</body></html>"""
        tool = WebSearchTool()
        results = tool._parse_ddg_html(html, 5)
        assert len(results) >= 1
        assert results[0]["title"] == "Example Title"
        assert results[0]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_format_results(self) -> None:
        tool = WebSearchTool()
        results = [
            {"title": "T1", "url": "https://a.com", "snippet": "S1"},
            {"title": "T2", "url": "https://b.com", "snippet": ""},
        ]
        formatted = tool._format_results(results)
        assert "T1" in formatted
        assert "https://a.com" in formatted
        assert "S1" in formatted
        assert "T2" in formatted
        assert "https://b.com" in formatted

    @pytest.mark.asyncio
    async def test_strip_html(self) -> None:
        tool = WebSearchTool()
        assert tool._strip_html("<b>bold</b> text") == "bold text"
        assert tool._strip_html("plain") == "plain"

    @pytest.mark.asyncio
    async def test_extract_link(self) -> None:
        tool = WebSearchTool()
        line = '<a href="https://x.com" class="result-link">X Site</a>'
        title, url = tool._extract_link(line)
        assert url == "https://x.com"
        assert "X Site" in title


class TestConsecutiveFailure:
    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self) -> None:
        registry = ToolRegistry()
        executor = ToolExecutor(
            registry, consecutive_failure_threshold=3, temp_dir=tempfile.mkdtemp()
        )

        class FlipTool(BaseTool):
            name: str = "failer"
            description: str = ""
            should_fail: bool = True

            async def execute(self, params: dict[str, Any], **kwargs: Any) -> str:
                if self.should_fail:
                    raise RuntimeError(params["msg"])
                return "ok"

        tool = FlipTool()
        registry.register_instance(tool)

        calls1 = [ToolCall(id="1", name="failer", arguments={"msg": "err1"})]
        w1 = await executor.execute(calls1)
        assert not w1[0].success

        calls2 = [ToolCall(id="2", name="failer", arguments={"msg": "err2"})]
        w2 = await executor.execute(calls2)
        assert not w2[0].success

        assert executor._failure_count.get("failer", 0) == 2

        # 成功后同一实例继续可用，失败计数必须重置
        tool.should_fail = False
        calls3 = [ToolCall(id="3", name="failer", arguments={})]
        w3 = await executor.execute(calls3)
        assert w3[0].success
        assert executor._failure_count.get("failer", 0) == 0

    @pytest.mark.asyncio
    async def test_consecutive_failures_pause_then_hard_stop(self) -> None:
        """-3 语义：

        阶段一（阈值内）真实执行→失败回喂，`should_abort=False`；
        阶段二（达阈值）**暂停**该工具、仍回喂失败结果与改用他法的指引，`should_abort=False`；
        阶段三（暂停后仍被反复调用 ≥ `_BLOCKED_RETRY_LIMIT` 次）→ 才 `should_abort=True` 硬中止。

        说明：本用例由旧契约「第 N 次即 abort」同步而来——旧断言钉的是已被替换的语义
        故按新设计升级（**不是删除**），并与常量同源以免硬编码漂移。
        """
        from bandi.tools.executor import _BLOCKED_RETRY_LIMIT

        registry = ToolRegistry()
        threshold = 3
        executor = ToolExecutor(
            registry, consecutive_failure_threshold=threshold, temp_dir=tempfile.mkdtemp()
        )
        registry.register_instance(FailTool())

        async def _call(i: int):
            results = await executor.execute(
                [ToolCall(id=str(i), name="failer", arguments={"msg": f"err{i}"})]
            )
            assert len(results) == 1
            return results[0]

        # 阶段一：真实执行失败（错误即工具自身报错 + 换路引导）——不得中止
        for i in range(threshold):
            result = await _call(i)
            assert not result.should_abort, f"call {i} 未达阈值不得中止"
            assert result.error.startswith(f"err{i}"), (
                f"call {i} 应回喂工具自身错误，实际 {result.error!r}"
            )
            assert "失败引导" in result.error, f"call {i} 应附换路引导，实际 {result.error!r}"

        # 阶段二：暂停但仍回喂（第 1..LIMIT-1 次被反复调用）——不得中止
        for hit in range(1, _BLOCKED_RETRY_LIMIT):
            result = await _call(threshold + hit - 1)
            assert not result.should_abort, f"暂停第 {hit} 次仍不得中止"
            assert "本轮已暂停该工具" in result.error, (
                f"应给出暂停与改用他法的指引：{result.error!r}"
            )

        # 阶段三：暂停后第 LIMIT 次反复调用 → 硬中止（防死循环烧钱）
        result = await _call(threshold + _BLOCKED_RETRY_LIMIT - 1)
        assert result.should_abort, "暂停后达到反复调用上限必须中止"
        assert "已中止本轮" in result.error, f"应为硬中止语义：{result.error!r}"

    @pytest.mark.asyncio
    async def test_unknown_tool_pause_then_abort_after_retries(self) -> None:
        """同上语义，未知工具（每次调用都失败）路径。"""
        from bandi.tools.executor import _BLOCKED_RETRY_LIMIT

        registry = ToolRegistry()
        threshold = 2
        executor = ToolExecutor(
            registry, consecutive_failure_threshold=threshold, temp_dir=tempfile.mkdtemp()
        )

        async def _call(i: int):
            results = await executor.execute([ToolCall(id=str(i), name="ghost_tool", arguments={})])
            assert len(results) == 1
            return results[0]

        for i in range(threshold):
            assert not (await _call(i)).should_abort, f"call {i} 未达阈值不得中止"
        for hit in range(1, _BLOCKED_RETRY_LIMIT):
            assert not (await _call(threshold + hit - 1)).should_abort, f"暂停第 {hit} 次仍不得中止"
        assert (await _call(threshold + _BLOCKED_RETRY_LIMIT - 1)).should_abort, (
            "暂停后达到反复调用上限必须中止"
        )


# ── EventBus Integration ──────────────────────────────────────────────


class TestToolEventBus:
    @pytest.mark.asyncio
    async def test_emits_tool_invoked_and_completed(self) -> None:
        bus = EventBus()
        events: list[str] = []

        async def collector(event):
            events.append(event.type.name)

        await bus.subscribe(EventType.TOOL_INVOKED, collector)
        await bus.subscribe(EventType.TOOL_COMPLETED, collector)
        await bus.subscribe(EventType.TOOL_ERROR, collector)

        registry = ToolRegistry()
        executor = ToolExecutor(registry, event_bus=bus, temp_dir=tempfile.mkdtemp())
        registry.register_instance(EchoTool())

        calls = [ToolCall(id="1", name="echo", arguments={"msg": "hi"})]
        await executor.execute(calls)

        assert "TOOL_INVOKED" in events
        assert "TOOL_COMPLETED" in events

    @pytest.mark.asyncio
    async def test_emits_tool_error_on_failure(self) -> None:
        bus = EventBus()
        events: list[str] = []

        async def collector(event):
            events.append(event.type.name)

        await bus.subscribe(EventType.TOOL_ERROR, collector)

        registry = ToolRegistry()
        executor = ToolExecutor(registry, event_bus=bus, temp_dir=tempfile.mkdtemp())
        registry.register_instance(FailTool())

        calls = [ToolCall(id="1", name="failer", arguments={"msg": "oops"})]
        await executor.execute(calls)

        assert "TOOL_ERROR" in events
