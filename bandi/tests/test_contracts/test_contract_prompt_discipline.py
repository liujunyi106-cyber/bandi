# 功能：契约 系统提示词的三条行为纪律与输出风格粒度
#
# 行为不变式：
#   ① 任务执行 / 工具使用 / 代码编辑三条纪律都必须进系统提示，且落在**静态段**
#      （跨轮/跨领域/跨模式字节稳定），不得混入动态段；
#   ② 工具使用纪律必须给跨工具策略：并行判定、有依赖才串行、不重复调用、
#      同一调用连续失败换路——只靠单工具 description 覆盖不到；
#   ③ 代码编辑规范必须给读后再改 / 最小改动 / 定点替换三条硬约束；
#   ④ 输出风格必须可判定：什么情况用表格、引用怎么写、什么时候不总结。
#
# 破坏者视角：把新段落塞进动态段（随领域/模式漂移），或在静态段里写入
# 会随运行状态变化的文本，下面用例必须红。

from __future__ import annotations

import pytest

from bandi.agent.prompt_harness import artifacts
from bandi.agent.prompt_harness.builder import PromptHarness
from bandi.agent.prompt_harness.prompt_loader import PromptLoader
from bandi.perception.context_loader import ContextBundle


def _bundle() -> ContextBundle:
    return ContextBundle(ontology_schema="## Ontology Schema\n(演示本体 schema)")


class TestDisciplinePromptsInStaticSection:
    def test_tool_usage_in_system_prompt(self) -> None:
        full = PromptHarness().build("execute", _bundle())
        assert artifacts.TOOL_USAGE_PROMPT in full, "工具使用纪律必须进系统提示"

    def test_code_edit_in_system_prompt(self) -> None:
        full = PromptHarness().build("execute", _bundle())
        assert artifacts.CODE_EDIT_PROMPT in full, "代码编辑规范必须进系统提示"

    def test_both_sections_live_in_static_not_dynamic(self) -> None:
        sections = PromptHarness().build_sections(mode="execute", context=_bundle())
        assert artifacts.TOOL_USAGE_PROMPT in sections.static
        assert artifacts.CODE_EDIT_PROMPT in sections.static
        assert artifacts.TOOL_USAGE_PROMPT not in sections.dynamic
        assert artifacts.CODE_EDIT_PROMPT not in sections.dynamic

    def test_static_prefix_byte_stable_across_domain_and_mode(self) -> None:
        """契约：新段落不得让静态前缀随领域/模式漂移（否则跨 Agent 缓存共享被打碎）。"""
        harness = PromptHarness()
        static_execute = harness.build_sections(
            mode="execute", context=_bundle(), domain_label="金融领域X"
        ).static
        static_plan = harness.build_sections(
            mode="plan", context=_bundle(), domain_label="医疗领域Y"
        ).static
        assert static_execute == static_plan
        assert "金融领域X" not in static_execute and "医疗领域Y" not in static_plan

    def test_static_cache_key_unaffected_by_new_sections(self) -> None:
        """破坏者视角：连发 70 个领域仍只有 1 条静态缓存（新段不引入键维度）。"""
        harness = PromptHarness()
        for index in range(70):
            harness.build_sections(mode="execute", context=_bundle(), domain_label=f"领域{index}")
        assert harness.static_cache_size == 1

    @pytest.mark.parametrize(
        "getter",
        ["get_task_execution", "get_tool_usage", "get_code_edit"],
    )
    def test_loader_getters_are_single_source(self, getter: str) -> None:
        expected = {
            "get_task_execution": artifacts.TASK_EXECUTION_PROMPT,
            "get_tool_usage": artifacts.TOOL_USAGE_PROMPT,
            "get_code_edit": artifacts.CODE_EDIT_PROMPT,
        }[getter]
        assert getattr(PromptLoader(), getter)() == expected


class TestToolUsageDisciplineContent:
    @pytest.mark.parametrize(
        "needle",
        [
            "一轮内并行",  # 何时并行
            "有依赖才串行",  # 何时不许并行
            "不重复调用",  # 避免冗余调用
            "先定位再细读",  # 检索顺序
            "连续两次失败就换路",  # 失败处理
            "工具结果会被截断或清理",  # 上下文清理告知
        ],
    )
    def test_tool_usage_covers_cross_tool_policy(self, needle: str) -> None:
        assert needle in artifacts.TOOL_USAGE_PROMPT

    def test_tool_usage_names_real_tools(self) -> None:
        """纪律里点名的检索工具必须是真实注册的工具名（写成不存在的工具等于没写）。"""
        for tool_name in ("grep", "glob", "list_dir", "tool_search", "read"):
            assert tool_name in artifacts.TOOL_USAGE_PROMPT


class TestCodeEditDisciplineContent:
    @pytest.mark.parametrize(
        "needle",
        [
            "改前先读",
            "最小改动",
            "定点替换优先",
            "不动无关内容",
            "改完自检",
        ],
    )
    def test_code_edit_covers_hard_constraints(self, needle: str) -> None:
        assert needle in artifacts.CODE_EDIT_PROMPT

    def test_write_requires_stated_reason(self) -> None:
        """整体重写（write）必须要求模型说明原因，否则「不覆盖」形同虚设。"""
        assert "整体重写" in artifacts.CODE_EDIT_PROMPT
        assert "说明" in artifacts.CODE_EDIT_PROMPT


class TestMemoryWordingDiscipline:
    def test_prompt_distinguishes_direct_write_from_draft(self) -> None:
        """契约：显式「记住」才可直接落库；随口陈述只能进草稿池，措辞不得越界。

        实测（2026-11-08）：用户说「我的习惯是先写测试」，模型回复「已沉淀到长期记忆」，
        实际只进了草稿池——话术与真实落点不一致会直接误导用户。"""
        prompt = artifacts.FRAMEWORK_PROMPT
        assert "已记住" in prompt
        assert "草稿池" in prompt
        assert "不要声称「已写入长期记忆」" in prompt


class TestOutputControlGranularity:
    @pytest.mark.parametrize(
        "needle",
        [
            "用列表还是表格",  # 何时表格
            "4 项以上",  # 可判定阈值
            "`路径:行号`",  # 引用格式
            "不另列「引用来源」清单",
            "标题层级最多到 ###",
            "长任务收尾只给三行",  # 什么时候不总结
            "不写客套开场",
        ],
    )
    def test_output_rules_are_executable(self, needle: str) -> None:
        assert needle in artifacts.OUTPUT_CONTROL_PROMPT

    def test_output_control_still_sinks_at_tail(self) -> None:
        """输出风格仍属收尾段（safety 显著性纪律），不得提前到静态段。"""
        sections = PromptHarness().build_sections(mode="execute", context=_bundle())
        assert artifacts.OUTPUT_CONTROL_PROMPT in sections.tail
        assert artifacts.OUTPUT_CONTROL_PROMPT not in sections.static
