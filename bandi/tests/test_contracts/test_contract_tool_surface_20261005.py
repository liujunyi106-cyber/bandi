# 功能：工具面修复批契约。
#
# 白名单裁掉，LLM 只能在正文里写步骤，无法落 todo；
# 有 request_user_input（with_always_available 已并），但真正调用会在
# 执行面被拒「not allowed for role tags」（schema 可见/执行被拒错位）；
# schema 面丢失 request_user_input（subagent_tool 真实透传 role_tool_tags）；
# 只给 date → 3 连败触发工具暂停（简单日期问题翻车）。
from __future__ import annotations

import pytest

from bandi.agent.mode_manager import ModeManager
from bandi.core.message import ToolCall
from bandi.tools.permissions_pipeline import PermissionsPipeline, plan_mode_tool_violations
from bandi.tools.registry import ToolRegistry


class TestPlanModeTodoWrite:
    """plan 白名单须含 todo_write（计划产物），且零安全违规、写盘工具仍拒。"""

    def test_todo_write_in_plan_allowlist(self) -> None:
        assert "todo_write" in ToolRegistry.PLAN_MODE_ALLOWED, (
            "plan 模式必须能写 todo 台账——否则计划模式无法产出结构化计划"
        )

    def test_mode_manager_keeps_todo_write_in_plan(self) -> None:
        mm = ModeManager(initial_mode="plan")
        kept = mm.filter_tools(["read", "write", "todo_write"])
        assert "todo_write" in kept and "write" not in kept, f"plan 过滤漂移：{kept}"

    def test_pipeline_check_mode_allows_todo_write(self) -> None:
        pipeline = PermissionsPipeline()
        verdict = pipeline._check_mode(
            ToolCall(id="t", name="todo_write", arguments={}), None, "plan"
        )
        assert verdict.allow, f"plan 执行期须放行 todo_write：{verdict.reason}"

    def test_registry_schema_includes_todo_write_in_plan(self) -> None:
        from bandi.tools.todo import TodoWriteTool

        reg = ToolRegistry()
        reg.register_instance(TodoWriteTool())
        names = [s["function"]["name"] for s in reg.list_schemas(mode="plan")]
        assert "todo_write" in names, f"plan schema 枚举须含 todo_write：{names}"

    def test_no_plan_safety_violation_for_todo_write(self) -> None:
        # 安全守卫（plan 白名单只读性）不得把 todo_write 误判为腐化成员
        from bandi.tools.todo import TodoWriteTool

        reg = ToolRegistry()
        reg.register_instance(TodoWriteTool())
        violations = plan_mode_tool_violations(reg)
        assert not [v for v in violations if "todo_write" in v], violations

    def test_breaker_write_still_denied_in_plan(self) -> None:
        # 破坏者：本修复不得打开写盘口子
        pipeline = PermissionsPipeline()
        verdict = pipeline._check_mode(ToolCall(id="w", name="write", arguments={}), None, "plan")
        assert not verdict.allow


class TestExecutorGateAlwaysAvailable:
    """执行面 role_gate 对全域交互通道豁免；非豁免工具仍按 tags 拒。"""

    @staticmethod
    def _executor(monkeypatch: pytest.MonkeyPatch, tags: list[str]):
        from bandi.tools.executor import ToolExecutor
        from bandi.tools.registry import ToolRegistry as _Reg
        from bandi.tools.todo import TodoReadTool
        from bandi.tools.user_input import UserInputTool

        monkeypatch.setattr(
            "bandi.tools.executor.ToolExecutor.__init__", lambda self, *a, **k: None
        )
        ex = ToolExecutor()
        reg = _Reg()
        reg.register_instance(UserInputTool())
        reg.register_instance(TodoReadTool())
        ex._registry = reg
        ex._role_tool_tags = list(tags)
        return ex, _Reg

    def test_always_available_exempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ex, _ = self._executor(monkeypatch, ["data"])  # 不含 util
        result = ex._role_gate(ToolCall(id="x", name="request_user_input", arguments={}))
        assert result is None, f"交互通道须豁免角色门（schema 可见/执行被拒错位）：{result}"

    def test_non_always_tool_still_gated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ex, _ = self._executor(monkeypatch, ["data"])  # todo_read tags=[util] 不匹配
        result = ex._role_gate(ToolCall(id="y", name="todo_read", arguments={}))
        assert result is not None and result.success is False


class TestSubagentSchemaChannelMerge:
    """子代理 schema 重算（受限角色）仍含交互通道——链与主链 factory 同源。"""

    def test_filtered_schema_keeps_always_available(self) -> None:
        from bandi.agent.subagent import _subagent_schema_allowlist
        from bandi.tools.base import BaseTool, ToolCategory
        from bandi.tools.user_input import UserInputTool

        class _DataTool(BaseTool):
            name = "data_tool"
            description = ""
            category = ToolCategory.DATA

            async def execute(self, params, context=None):
                return "ok"

        reg = ToolRegistry()
        reg.register_instance(UserInputTool())
        reg.register_instance(_DataTool())
        allowed = _subagent_schema_allowlist(None, reg, ["data"])
        assert allowed is not None and "request_user_input" in allowed, (
            f"受限角色子代理 schema 必须保留交互通道：{allowed}"
        )
        assert "data_tool" in allowed

    def test_breaker_full_tags_keep_business_tools(self) -> None:
        from bandi.agent.subagent import _subagent_schema_allowlist
        from bandi.tools.base import BaseTool, ToolCategory

        class _DataTool(BaseTool):
            name = "data_tool"
            description = ""
            category = ToolCategory.DATA

            async def execute(self, params, context=None):
                return "ok"

        reg = ToolRegistry()
        reg.register_instance(_DataTool())
        allowed = _subagent_schema_allowlist(["data_tool"], reg, ["data"])
        assert allowed is not None and "data_tool" in allowed


class TestSanitizerTruncationActionable:
    """OutputSanitizer 截断提示必须可行动（旧「请查看日志」模型无从执行）。"""

    def test_truncation_message_actionable(self) -> None:
        from bandi.safety.output_sanitizer import OutputSanitizer, OutputSanitizerConfig

        sanitizer = OutputSanitizer(OutputSanitizerConfig(max_chars=50))
        out = sanitizer.sanitize("x" * 100)
        assert out.endswith("]"), out[-60:]
        assert "查看日志" not in out, "截断提示不得指向模型不可见的日志"
        assert "read" in out or "分段" in out, f"截断提示须给模型可行动出路：{out[-80:]}"
        assert "缩小输出范围" in out or "head" in out, (
            f"非文件来源（bash/MCP）也须有可行动出路：{out[-120:]}"
        )


class TestHallucinationJudgePromptExemption:
    """judge 指令契约——必须显式豁免问候/自我介绍。

    首轮问候「你好！我是 Bandi 助手…」被 judge 判 0.8 幻觉
    → SAFETY_BLOCKED → assistant 回复不落库（刷新即丢）。系统身份无法用对话
    上下文核验，不属事实断言，judge 指令必须显式排除。
    """

    @pytest.mark.asyncio
    async def test_judge_prompt_exempts_greeting_and_selfintro(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        import bandi.llm.accounted as accounted_mod
        from bandi.eval import hallucination_checker as hc_mod

        captured: dict = {}

        async def fake_complete(provider: object, messages: list, **kwargs: object):
            captured["messages"] = messages
            return SimpleNamespace(
                content='{"hallucination_score": 0.0, "hallucination_spans": []}'
            )

        monkeypatch.setattr(accounted_mod, "accounted_complete", fake_complete)
        report = await hc_mod.l2_check(
            "你好！我是 Bandi 助手，有什么可以帮你的吗？",
            "[本轮用户输入] 你好，先简单打个招呼即可。",
            provider=object(),
        )
        blob = " ".join(str(m.get("content", "")) for m in captured["messages"])
        assert "自我介绍" in blob and "不计入" in blob, (
            "judge 指令必须显式豁免问候/自我介绍（真机误杀根修）"
        )
        assert report.hallucination_score == 0.0


class TestDateCalculatorDifferenceDefault:
    """difference 缺 date2 缺省=今天；显式 date2 行为不变。"""

    @pytest.mark.asyncio
    async def test_difference_defaults_date2_to_today(self) -> None:
        from datetime import date as _date

        from bandi.tools.util.date_calculator import DateCalculatorTool

        tool = DateCalculatorTool()
        result = await tool.execute({"operation": "difference", "date": "2033-07-15"})
        today = _date.today()
        assert result["date1"] == today.isoformat(), "缺省时起点=今天（从今天到目标）"
        assert result["date2"] == "2033-07-15"
        assert result["date2_defaulted"] is True
        assert result["difference"]["days"] == (_date(2033, 7, 15) - today).days

    @pytest.mark.asyncio
    async def test_explicit_date2_unchanged_and_not_flagged(self) -> None:
        from bandi.tools.util.date_calculator import DateCalculatorTool

        tool = DateCalculatorTool()
        result = await tool.execute(
            {"operation": "difference", "date": "2026-01-01", "date2": "2026-01-08"}
        )
        assert result["difference"]["days"] == 7
        assert result.get("date2_defaulted") is not True

    @pytest.mark.asyncio
    async def test_breaker_missing_date_still_errors(self) -> None:
        from bandi.tools.util.date_calculator import DateCalculatorTool

        tool = DateCalculatorTool()
        with pytest.raises(ValueError):
            await tool.execute({"operation": "difference"})
