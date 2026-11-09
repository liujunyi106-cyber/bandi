# 功能：契约——预算硬帽：a) 动态段 per-source 硬帽（单一来源不得吃满池）；
# b) 工具分档表默认非空；c) 单轮工具结果聚合帽（最大结果落盘预览、小结果完整、读类豁免）。
from __future__ import annotations

from typing import Any

from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.core.config import BandiConfig
from bandi.core.message import ToolResult
from bandi.tools.executor import ToolExecutor
from bandi.tools.registry import ToolRegistry


class TestPerSourceCaps:
    def test_huge_rules_do_not_starve_memory(self) -> None:
        """破坏者视角：巨型规则不得把记忆挤没
        （原共享池下记忆只剩保底甚至被语义裁空）。"""
        max_tokens = 1000
        rules = "规则内容甲乙丙丁。" * 300
        memory = "关键记忆条目：必须保留XYZ。"
        out = PromptHarness._trim_project_sections(rules, memory, max_tokens)
        assert "关键记忆条目" in out, f"记忆被规则挤没：{out[:200]!r}"

    def test_rules_and_memory_within_caps(self) -> None:
        """契约：两源都超长时各自被裁到池的 ~60% 以内。"""
        from bandi.utils.token_counter import TokenCounter

        max_tokens = 1000
        rules = "规则甲乙丙。" * 500
        memory = "记忆戊己庚。" * 500
        out = PromptHarness._trim_project_sections(rules, memory, max_tokens)
        cap = int(max_tokens * 0.6) + 10
        assert TokenCounter.estimate(out) <= max_tokens + 10, "总池不得超"
        rules_part, sep, memory_part = out.partition("记忆戊己庚")
        assert TokenCounter.estimate(rules_part) <= cap, "规则超 per-source 帽"
        assert TokenCounter.estimate(memory_part) <= cap, "记忆超 per-source 帽"


class TestToolPerToolBudget:
    def test_default_per_tool_table_nonempty(self) -> None:
        """契约：分档表默认非空且读类为大档（原默认空=全统一 8192）。"""
        table = BandiConfig().tools.per_tool_max_output_tokens
        assert table, "默认分档表不得为空"
        assert table.get("read", 0) >= 20000
        assert table.get("grep", 0) >= 20000
        assert table.get("bash", 0) > 8192


def _executor(tmp_path: Any) -> ToolExecutor:
    return ToolExecutor(registry=ToolRegistry(), temp_dir=str(tmp_path))


def _result(name: str, size: int) -> ToolResult:
    return ToolResult(tool_call_id="x", tool_name=name, success=True, output="Z" * size, error="")


class TestBatchAggregateBudget:
    def test_batch_over_cap_persists_largest_small_kept(self, tmp_path: Any) -> None:
        """契约（核心）：超帽按最大者落盘预览（非均摊截断），小结果完整。"""
        ex = _executor(tmp_path)
        big = _result("bash", 60000)
        small = _result("bash", 100)
        ex._enforce_batch_output_budget([big, small])
        assert len(big.output) < 60000, "最大结果必须被预览替换"
        assert "[完整输出:" in big.output, "必须附完整输出路径指引"
        assert small.output == "Z" * 100, "小结果必须完整保留"

    def test_read_exempt_from_aggregate(self, tmp_path: Any) -> None:
        """契约：读类（read/grep/glob）豁免聚合帽（其语义为按需分页读）。"""
        ex = _executor(tmp_path)
        big_read = _result("read", 60000)
        ex._enforce_batch_output_budget([big_read])
        assert len(big_read.output) == 60000, "读类豁免"

    def test_under_cap_untouched(self, tmp_path: Any) -> None:
        """契约（对照）：总量未超帽时零改动。"""
        ex = _executor(tmp_path)
        r = _result("bash", 1000)
        ex._enforce_batch_output_budget([r])
        assert len(r.output) == 1000
