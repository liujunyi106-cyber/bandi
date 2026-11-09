# 功能：交互通道全域可用契约
#
# 钉行为：领域白名单（perception/domains.yaml 的 allowed_tools）只裁剪**业务能力**；
# 交互通道 request_user_input 必须始终可见——否则 Agent 无法向用户提问
from __future__ import annotations

from typing import Any

from bandi.perception.domain_router import DomainDef
from bandi.tools.registry import ToolRegistry


class TestAlwaysAvailable:
    def test_constant_declares_user_input(self) -> None:
        assert "request_user_input" in ToolRegistry.ALWAYS_AVAILABLE_TOOLS

    def test_no_domain_whitelist_excludes_it(self) -> None:
        # 破坏者视角：任何 domain 的 allowed_tools 都不该含它（否则两处口径会漂移）
        from pathlib import Path

        import yaml

        path = Path(__file__).resolve().parents[2] / "bandi" / "perception" / "domains.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key, dom in (data.get("domains") or {}).items():
            assert "request_user_input" not in (dom.get("allowed_tools") or []), (
                f"domain {key} 不应显式列 request_user_input（由 ALWAYS_AVAILABLE_TOOLS 统一并入）"
            )


class TestPlanModeAllowlist:
    """plan 模式执行期白名单（PermissionsPipeline._check_mode 的唯一判据）。

     LLM 已按要求调用 request_user_input（参数带 3 组
    choices 全部正确），却被 _check_mode 拒为「not allowed in plan mode」——
    with_always_available() 只并入了 schema/角色/领域三处过滤面，漏了执行期白名单。
    """

    @staticmethod
    def _plan_allows(tool_name: str) -> bool:
        # PLAN_MODE_TOOLS 是纯只读集（plan_mode_tool_violations 守卫专用），不含交互通道。
        return tool_name in ToolRegistry.PLAN_MODE_ALLOWED

    def test_user_input_is_in_plan_allowlist(self) -> None:
        assert self._plan_allows("request_user_input"), (
            "交互通道必须在 plan 白名单内——否则 plan 模式下提问工具恒被拒，"
            "多选项卡/多问分页卡在生产链路永不触发"
        )

    def test_every_always_available_tool_is_in_plan_allowlist(self) -> None:
        # 族级覆盖：新增 ALWAYS_AVAILABLE_TOOLS 成员时漏并入 plan 白名单即红
        for name in ToolRegistry.ALWAYS_AVAILABLE_TOOLS:
            assert self._plan_allows(name), f"{name} 未并入 PLAN_MODE_ALLOWED"

    def test_plan_mode_tools_itself_stays_readonly(self) -> None:
        # 反向口径：PLAN_MODE_TOOLS 必须保持「纯只读集」——plan_mode_tool_violations
        # 守卫遍历它判腐化，混入交互通道会被误判为违规成员。
        assert "request_user_input" not in ToolRegistry.PLAN_MODE_TOOLS

    def test_pipeline_check_mode_allows_user_input(self) -> None:
        # 破坏者视角：走真实 PermissionsPipeline._check_mode（生产判据本身）
        from bandi.core.message import ToolCall
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        pipeline = PermissionsPipeline()
        verdict = pipeline._check_mode(
            ToolCall(id="tc-1", name="request_user_input", arguments={}), None, "plan"
        )
        assert verdict.allow, f"plan 模式应放行交互通道：{verdict.reason}"

    def test_write_still_denied_in_plan_mode(self) -> None:
        # 反向不得误伤：plan 模式对写工具仍须硬拒（否则本修复打开大洞）
        from bandi.core.message import ToolCall
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        pipeline = PermissionsPipeline()
        verdict = pipeline._check_mode(
            ToolCall(id="tc-1", name="write", arguments={}), None, "plan"
        )
        assert not verdict.allow

    def test_execute_mode_allowlist_unaffected(self) -> None:
        # execute 模式不受 plan 白名单影响（口径不外溢）
        from bandi.core.message import ToolCall
        from bandi.tools.permissions_pipeline import PermissionsPipeline

        pipeline = PermissionsPipeline()
        verdict = pipeline._check_mode(
            ToolCall(id="tc-1", name="write", arguments={}), None, "execute"
        )
        assert verdict.allow


class TestPipelineMerge:
    @staticmethod
    def _merge(allowed: list[str]) -> list[str]:
        """功能：复刻 pipeline 的并入逻辑。

        非空域白名单 ∪ 全域交互通道；**空名单 = 不限制**→ 原样返回。"""
        if not allowed:
            return allowed
        for name in ToolRegistry.ALWAYS_AVAILABLE_TOOLS:
            if name not in allowed:
                allowed = [*allowed, name]
        return allowed

    def test_domain_allowlist_gets_user_input(self) -> None:
        # 领域限定 [read, write] → 合并后必须含 request_user_input
        allowed = self._merge(["read", "write"])
        assert "request_user_input" in allowed
        assert {"read", "write"}.issubset(set(allowed))

    def test_domain_with_empty_allowlist_stays_unrestricted(self) -> None:
        # 空名单 = 不限制：**不得**并入成 ["request_user_input"]。
        # `current_tool_names` 按「非空才筛」判定 ⇒ 不限制退化为「只允许 1 个工具」
        # ⇒ 首轮工具面工件恒空、artifact_hashes 恒 {}、第二轮 Delta 头永不出现
        # （test_session_restore_complete 两用例红）。
        assert self._merge([]) == []


class TestEmptyWhitelistKeepsFullToolSurface:
    """回归守卫：空域白名单下工具面工件必须照常产出（不得被交互通道并入压成单工具）。"""

    @staticmethod
    def _assembler_with(tools: list[Any]) -> Any:
        from bandi.perception.context_assembler import ContextAssembler
        from bandi.tools.registry import ToolRegistry

        registry = ToolRegistry()
        for name in tools:
            registry.register_instance(_StubTool(name))
        return ContextAssembler(tool_registry=registry)

    def test_empty_allowlist_is_unrestricted_for_artifact(self) -> None:
        assembler = self._assembler_with(["alpha", "beta"])
        artifact = assembler._build_tool_surface_artifact([])
        assert artifact is not None, "空白名单=不限制，工件必须产出（否则 Delta 链断）"
        assert "alpha" in artifact.content and "beta" in artifact.content

    def test_nonempty_allowlist_filters_but_keeps_artifact(self) -> None:
        assembler = self._assembler_with(["alpha", "beta"])
        artifact = assembler._build_tool_surface_artifact(["alpha"])
        assert artifact is not None
        assert "alpha" in artifact.content and "beta" not in artifact.content

    def test_current_tool_names_honours_unrestricted(self) -> None:
        assembler = self._assembler_with(["alpha", "beta"])
        assert assembler.current_tool_names([]) == ["alpha", "beta"]


class _StubTool:
    """功能：最小工具桩（仅供工具面工件测试；只需注册期校验所需字段）。"""

    def __init__(self, name: str) -> None:
        from bandi.tools.base import ToolCategory

        self.name = name
        self.description = f"{name} 工具"
        self.category = ToolCategory.UTIL
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}
        self.tags: list[str] = []
        self.enabled = True


def test_domain_def_default_allows_nothing() -> None:
    # 口径锚点：DomainDef.allowed_tools 默认空（由 pipeline 统一并入交互通道）
    dom = DomainDef(key="x", label="X")
    assert dom.allowed_tools == []


class TestPlanModePrompt:
    """plan 模式提示词不得诱导预估工时，且须显式放行向用户提问。

    该约束此前被 YAML 影子漏掉（yaml 里plan 仍写「预期耗时」且未放行提问），
    现常量为唯一真相源，从 loader 取值即真实生效文本。
    """

    def test_plan_prompt_has_no_time_estimate(self) -> None:
        from bandi.agent.prompt_harness.prompt_loader import PromptLoader

        plan = PromptLoader().get_mode_prompts()["plan"]
        assert "预期耗时" not in plan
        assert "request_user_input" in plan, "plan 模式必须显式放行向用户提问"
