# 功能：契约——工具结果摘要三层。
# 钉行为：规则摘要三类（shell errors+tail / read head+stats / 其他首段 + 字符帽）/
# [tool:summary] 格式 / 幂等护栏 / LLM 白名单+阈值+失败回退（spy）/ executor 截断点
# 摘要 + 落盘路径回传（含全文产物断言）/ prune 摘要化替换 + skill 工具豁免（名单修复）。
from __future__ import annotations

from pathlib import Path

import pytest

from bandi.core.message import Message, MessageRole, ToolCall
from bandi.memory.compression_pipeline import prune_before_request
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry
from bandi.tools.result_summary import (
    SUMMARY_MAX_CHARS,
    is_already_shaped,
    rule_summary,
    summarize_tool_output,
)


class _StaticOutputTool(BaseTool):
    name = "read"

    def __init__(self, output: str) -> None:
        super().__init__()
        self._output = output

    async def execute(self, params: dict, context: object | None = None) -> str:
        return self._output


# ──────────────────────────────────────────────────────────────────────
# A. 规则摘要族
# ──────────────────────────────────────────────────────────────────────


class TestRuleSummary:
    def test_shell_summary_extracts_errors_and_tail(self) -> None:
        lines = [f"line-{i:03d}" for i in range(1, 51)]
        lines[10] = "ValueError: something failed"
        lines[20] = "Traceback (most recent call last):"
        content = "\n".join(lines)
        summary = rule_summary(content, "bash")
        assert summary.startswith("[tool:summary] tool=bash")
        assert "errors:" in summary
        assert "ValueError: something failed" in summary
        assert "Traceback" in summary
        assert "line-050" in summary, "tail 必须覆盖最后 30 行（含末行）"
        assert "line-001" not in summary

    def test_shell_summary_without_errors_keeps_tail(self) -> None:
        content = "\n".join(f"out-{i}" for i in range(1, 60))
        summary = rule_summary(content, "bash")
        assert "errors:" not in summary
        assert "out-59" in summary

    def test_read_summary_head_and_stats(self) -> None:
        content = "\n".join(f"row-{i:02d}" for i in range(1, 101))
        summary = rule_summary(content, "read")
        assert summary.startswith("[tool:summary] tool=read")
        assert "row-01" in summary
        assert "row-12" in summary
        assert "row-13" not in summary
        assert "(100 lines," in summary

    def test_read_summary_head_capped(self) -> None:
        first = "A" * 1000
        content = first + "\n" + "\n".join(f"row-{i}" for i in range(2, 20))
        summary = rule_summary(content, "read")
        assert "A" * 600 in summary
        assert "A" * 601 not in summary

    def test_generic_summary_capped_at_max_chars(self) -> None:
        content = "z" * 5000
        summary = rule_summary(content, "write")
        assert len(summary) <= SUMMARY_MAX_CHARS
        assert "(5,000 chars)" in summary or "chars)" in summary

    def test_is_already_shaped_guard(self) -> None:
        assert is_already_shaped("[tool:summary] tool=bash\ncore: x") is True
        assert is_already_shaped("[Old result cleared]") is True
        assert is_already_shaped("") is True
        assert is_already_shaped("[full output persisted: /tmp/x.txt — use read]") is True
        assert is_already_shaped("normal tool output") is False


# ──────────────────────────────────────────────────────────────────────
# B. LLM 摘要族（spy）
# ──────────────────────────────────────────────────────────────────────


class _SpyLLM:
    def __init__(self, result: str = "LLM-CORE", fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = result
        self._fail = fail

    async def __call__(self, prompt: str, session_id: str) -> str:
        self.calls.append((prompt, session_id))
        if self._fail:
            raise RuntimeError("llm down")
        return self._result


class TestLLMSummary:
    @pytest.mark.asyncio
    async def test_llm_not_called_below_threshold(self) -> None:
        spy = _SpyLLM()
        summary = await summarize_tool_output("short output", "bash", llm=spy, session_id="s")
        assert spy.calls == []
        assert summary.startswith("[tool:summary] tool=bash")

    @pytest.mark.asyncio
    async def test_llm_called_for_whitelist_over_threshold(self) -> None:
        spy = _SpyLLM(result="CORE-TEXT")
        content = "x" * 9000
        summary = await summarize_tool_output(content, "bash", llm=spy, session_id="s-1")
        assert len(spy.calls) == 1
        assert spy.calls[0][1] == "s-1"
        assert "CORE-TEXT" in summary
        assert summary.startswith("[tool:summary] tool=bash")

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_rule(self) -> None:
        spy = _SpyLLM(fail=True)
        content = "x" * 9000
        summary = await summarize_tool_output(content, "bash", llm=spy, session_id="s")
        assert "tail:" in summary, "LLM 失败必须回退规则摘要"

    @pytest.mark.asyncio
    async def test_llm_not_called_for_non_whitelist_tool(self) -> None:
        spy = _SpyLLM()
        summary = await summarize_tool_output("x" * 9000, "write", llm=spy, session_id="s")
        assert spy.calls == []
        assert summary.startswith("[tool:summary] tool=write")


# ──────────────────────────────────────────────────────────────────────
# C. executor 截断点族
# ──────────────────────────────────────────────────────────────────────


class TestExecutorSummary:
    def _executor(self, tmp_path: Path, tool: BaseTool, **kwargs) -> ToolExecutor:
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        class _AutoApprove:
            async def ask_permission(self, tool_name: str, args: dict, risk: str) -> bool:
                return True

            async def ask_confirmation(self, prompt: str, timeout: float = 30.0) -> bool:
                return True

            async def ask_input(self, prompt: str, *, secret: bool = False) -> str:
                return ""

            async def show_notification(self, message: str, level: str = "info") -> None:
                return None

        pipeline = PermissionsPipeline()
        pipeline.set_user_interaction(_AutoApprove())
        registry = ToolRegistry()
        registry.register_instance(tool)
        return ToolExecutor(
            registry=registry,
            permissions_pipeline=pipeline,
            temp_dir=str(tmp_path / "tmp"),
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_large_output_summarized_and_persisted(self, tmp_path: Path) -> None:
        # 端到端：截断点走摘要 + 落盘路径回传 + 落盘文件逐字保全（最终产物断言）
        content = "y" * 9000
        executor = self._executor(tmp_path, _StaticOutputTool(content), max_output_tokens=2000)
        (result,) = await executor.execute(
            [ToolCall(id="c1", name="read", arguments={"path": "probe.txt"})]
        )
        assert result.success
        assert result.output.startswith("[tool:summary] tool=read")
        assert "[full output persisted:" in result.output
        assert "...\n[truncated]" not in result.output

        import re

        match = re.search(r"\[full output persisted: (.+?) —", result.output)
        assert match is not None
        dump_path = Path(match.group(1))
        assert dump_path.is_file()
        assert dump_path.read_text(encoding="utf-8") == content, "落盘必须保存全文"

    @pytest.mark.asyncio
    async def test_llm_summary_wired_in_executor(self, tmp_path: Path) -> None:
        spy = _SpyLLM(result="EXEC-LLM-CORE")
        executor = self._executor(
            tmp_path,
            _StaticOutputTool("z" * 9000),
            max_output_tokens=2000,
            summary_llm=spy,
        )
        executor.bind_session_meta({"session_id": "s-exec"})
        (result,) = await executor.execute(
            [ToolCall(id="c1", name="read", arguments={"path": "probe.txt"})]
        )
        assert result.success
        assert "EXEC-LLM-CORE" in result.output
        assert spy.calls and spy.calls[0][1] == "s-exec"

    @pytest.mark.asyncio
    async def test_small_output_untouched(self, tmp_path: Path) -> None:
        executor = self._executor(tmp_path, _StaticOutputTool("small ok"), max_output_tokens=2000)
        (result,) = await executor.execute(
            [ToolCall(id="c1", name="read", arguments={"path": "probe.txt"})]
        )
        assert result.output == "small ok"


# ──────────────────────────────────────────────────────────────────────
# D. prune 族（compression_pipeline）
# ──────────────────────────────────────────────────────────────────────


def _tool_msg(name: str, content: str) -> Message:
    return Message(role=MessageRole.TOOL, content=content, tool_call_id=f"c-{name}", name=name)


class TestPruneSummary:
    def _messages(self, big_tool_name: str, big_content: str) -> list[Message]:
        return [
            Message(role=MessageRole.USER, content="u1"),
            _tool_msg(big_tool_name, big_content),
            Message(role=MessageRole.USER, content="u2"),
            _tool_msg("bash", "small"),
            Message(role=MessageRole.USER, content="u3"),
            _tool_msg("bash", "small"),
        ]

    def test_prune_replaces_with_persisted_excerpt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_TOOL_OUTPUT_DIR", str(tmp_path / "outputs"))
        messages = self._messages("bash", "错误" * 100000)
        pruned, trimmed = prune_before_request(messages)
        assert trimmed == 1
        content = pruned[1].content or ""
        assert content.startswith("[tool:persisted] tool=bash"), "超预算旧输出改为真实正文摘录"
        assert "[full output persisted:" in content, "必须带全文路径（信息不丢）"
        assert content != "[Old result cleared]"

    def test_prune_protects_skill_tools(self) -> None:
        # 破坏者：skill_invoke 大输出必须豁免（修复前名单 {"skill"} 不匹配真实工具名）
        content = "错误" * 100000
        messages = self._messages("skill_invoke", content)
        pruned, trimmed = prune_before_request(messages)
        assert trimmed == 0
        assert pruned[1].content == content, "skill_invoke 输出不得被剪枝"

    def test_prune_idempotent_for_shaped_content(self) -> None:
        shaped = "[tool:summary] tool=bash\ncore: " + "错误" * 100000
        messages = self._messages("bash", shaped)
        pruned, trimmed = prune_before_request(messages)
        assert trimmed == 0
        assert pruned[1].content == shaped


class TestMicroTrimInteraction:
    def test_micro_trim_skips_shaped_content(self) -> None:
        # 破坏者：已摘要内容（[tool:summary]）不得被 micro 头尾截断二次破坏；
        # 普通长内容仍走既有 micro 截断（对照）
        from bandi.memory.compression_pipeline import MicroCompressor

        shaped = "[tool:summary] tool=read\ncore: " + "错" * 800
        plain = "y" * 1000
        messages = [
            Message(role=MessageRole.USER, content="u"),
            _tool_msg("read", shaped),
            _tool_msg("read", plain),
        ]
        msgs, _stats = MicroCompressor().compress(messages)
        assert msgs[1].content == shaped, "已摘要内容不得被 micro 二次截断"
        assert "[已截断" not in msgs[1].content
        assert "[已截断" in msgs[2].content, "普通长内容仍走 micro 头尾截断（既有行为）"
