"""
分段承载：ContextBundle 将 project_instructions（规则）与 memory_context（层级记忆）
   分离，builder 渲染时两者都存在（不再把记忆追加到规则末尾被后缀裁剪优先丢弃）。
规则优先 + 记忆保留：规则放得下时，尽量装层级记忆（语义装箱），绝不「规则撑
   满预算后记忆整段丢失」——大 AGENTS.md + 层级记忆场景下 system prompt 仍含记忆
相对顺序：输出 规则 → 记忆，不被颠倒。
规则本身超预算：退规则后缀裁剪（_trim_project_instructions 兼容保留）。
"""

from __future__ import annotations

from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.utils.token_counter import TokenCounter


def _link(rules: str, memory: str) -> str:
    if rules and memory:
        return f"{rules}\n\n{memory}"
    return rules or memory


class TestSectionTrim:
    def test_both_present_within_budget(self) -> None:
        """/预算充足时规则与记忆都完整保留且顺序 规则→记忆。"""
        rules = "AGENTS：使用中文回复。"
        memory = "【员工层】今日已完成选品方法论进展汇报。"
        out = PromptHarness._trim_project_sections(rules, memory, 500)
        assert "使用中文回复" in out
        assert "选品方法论进展" in out
        assert TokenCounter.count(out) <= 500
        assert out.index("使用中文回复") < out.index("选品方法论进展")

    def test_large_rules_memory_not_wholly_dropped(self) -> None:
        """
        用语义装箱保住记忆首块，而非规则撑满后记忆为 0。"""
        rules = "项目指令。" * 200  # 大规则
        memory = "【会话层】这是最重要的会话进展记忆 包含关键决策。"
        max_tokens = 80
        out = PromptHarness._trim_project_sections(rules, memory, max_tokens)
        assert TokenCounter.count(out) <= max_tokens
        # 记忆关键内容不应被整段丢弃（若 memory 全被裁则此断言失败）
        assert "会话层" in out or "关键决策" in out

    def test_memory_semantic_packing_keeps_first_block(self) -> None:
        """记忆做语义装箱而非裸后缀——多块记忆在预算不足时保住首块 + 首行标识。"""
        rules = "短规则"
        memory = (
            "【员工层】员工最终产出：完成了 X 系统的架构设计。\n\n"
            "【项目层】项目长期经验：灰度发布前必须备份。\n\n"
            "【全局层】通用经验：多 Agent 协作先对齐目标。"
        )
        out = PromptHarness._trim_project_sections(rules, memory, 60)
        assert out
        # 优先保留最前的员工层/项目层块（高优先级在 memory 中排前，首个语义块必被尝试装入）
        assert "最终产出" in out or "架构设计" in out

    def test_rules_alone_exceed_budget_falls_back_suffix(self) -> None:
        """规则本身超预算——退规则后缀裁剪，无剩余给记忆。"""
        rules = "规则内容。" * 300
        memory = "【会话层】记忆甲：内容"
        max_tokens = 40
        out = PromptHarness._trim_project_sections(rules, memory, max_tokens)
        assert TokenCounter.count(out) <= max_tokens
        assert "规则内容" in out

    def test_no_budget_keeps_both_linked(self) -> None:
        rules = "规则R"
        memory = "记忆M"
        assert PromptHarness._trim_project_sections(rules, memory, None) == "规则R\n\n记忆M"

    def test_empty_memory_rules_only(self) -> None:
        rules = "规则R"
        assert PromptHarness._trim_project_sections(rules, "", 50) == "规则R"


class TestBuildIntegration:
    def test_build_includes_memory_context(self) -> None:
        """build() 渲染 ContextBundle 时规则与记忆都在 system prompt。"""
        from bandi.perception.context_loader import ContextBundle

        ctx = ContextBundle(
            project_instructions="AGENTS：使用中文。",
            memory_context="【会话层】关键会话进展标记。",
        )
        prompt = PromptHarness().build("execute", ctx, project_instructions_max_tokens=4096)
        assert "AGENTS：使用中文" in prompt
        assert "关键会话进展标记" in prompt

    def test_backward_compat_trim_project_instructions(self) -> None:
        """兼容：_trim_project_instructions 仍可用（既有契约不破坏）。"""
        long_cn = "项目记忆说明。" * 200
        out = PromptHarness._trim_project_instructions(long_cn, 50)
        assert TokenCounter.count(out) <= 50
