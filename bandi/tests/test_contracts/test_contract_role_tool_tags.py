# 功能：契约 — OrgRole.tool_tags role-scoped 工具过滤（schema 注入 + executor 门控双路径 fail-closed）
from __future__ import annotations

from typing import Any

import pytest

from bandi.tools.base import BaseTool
from bandi.tools.registry import ToolRegistry


class _TaggedTool(BaseTool):
    def __init__(self, name: str, tags: list[str]) -> None:
        self.name = name
        self.description = f"tool-{name}"
        self.tags = tags
        self.parameters = {"type": "object", "properties": {}}

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "ok"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_instance(_TaggedTool("web_search", ["web", "util"]))
    registry.register_instance(_TaggedTool("bash", ["code"]))
    registry.register_instance(_TaggedTool("read", ["util"]))
    return registry


class TestOrgRoleToolTagsField:
    def test_default_empty_and_roundtrip(self) -> None:
        from bandi.company.org_engine import OrgEngine

        engine = OrgEngine()
        engine.register_role(
            "analyst2",
            "Analyst II",
            reports_to="cto",
            can_spawn=[],
            tool_tags=["web", "data"],
        )
        role = engine.get_role("analyst2")
        assert role is not None and role.tool_tags == ["web", "data"], (
            "register_role 必须接受 tool_tags"
        )
        payload = engine._role_to_dict(role)
        restored = engine._role_from_dict(payload)
        assert restored.tool_tags == ["web", "data"], "序列化往返必须保留 tool_tags"

    def test_persist_restore_keeps_tool_tags(self) -> None:
        from bandi.company.org_engine import OrgEngine

        class _Store:
            def __init__(self) -> None:
                self.roles: list[dict[str, Any]] = []
                self.employees: list[dict[str, Any]] = []

            def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
                self.roles = roles

            def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
                self.employees = employees

            def load_org_roles(self) -> list[dict[str, Any]]:
                return self.roles

            def load_org_employees(self) -> list[dict[str, Any]]:
                return self.employees

        store = _Store()
        engine = OrgEngine(persist_store=store)
        engine.register_role("dev", "Dev", reports_to="cto", tool_tags=["code"])
        engine2 = OrgEngine(persist_store=store)
        role = engine2.get_role("dev")
        assert role is not None and role.tool_tags == ["code"], (
            "persist/restore 双路必须透传 tool_tags"
        )

    def test_set_reports_to_keeps_tool_tags(self) -> None:
        from bandi.company.org_engine import OrgEngine

        engine = OrgEngine()
        engine.register_role("qa", "QA", reports_to="em", tool_tags=["util"])
        engine.set_reports_to("qa", "cto")
        role = engine.get_role("qa")
        assert role is not None and role.tool_tags == ["util"], (
            "set_reports_to 重建不得丢 tool_tags"
        )


class TestSchemaInjectionFilter:
    """生产路径 1：factory schema 注入面按 role_tool_tags 裁剪 active_tools。"""

    @staticmethod
    def _filtered_names(registry: ToolRegistry, role_tool_tags: list[str] | None) -> set[str]:
        from bandi.agent.factory import filter_tools_by_role_tags

        names = [t.name for t in registry.list_tools()]
        filtered = filter_tools_by_role_tags(names, registry, role_tool_tags)
        return set(filtered)

    def test_tags_narrow_toolset(self) -> None:
        names = self._filtered_names(_registry(), ["web"])
        assert names == {"web_search"}, "role_tool_tags=['web'] 必须只留 web 标签工具"

    def test_none_keeps_all_and_empty_denies(self) -> None:
        """None=不过滤（user 默认态）；空列表=显式全禁（fail-closed 语义
        与 role_filter 契约「查询异常返回空名单 = 全禁」对齐）——.6 收口：
        原执行面把空列表折叠为不过滤，使该 fail-closed 声明在生产不成立。"""
        registry = _registry()
        all_names = {t.name for t in registry.list_tools()}
        assert self._filtered_names(registry, None) == all_names, "None = 不过滤（user 默认态）"
        assert self._filtered_names(registry, []) == set(), "空列表 = 全禁（fail-closed）"


class TestExecutorGate:
    """生产路径 2：executor 执行入口按 role_tool_tags fail-closed（防幻觉调用未注入工具）。"""

    @pytest.mark.asyncio
    async def test_executor_rejects_out_of_scope_tool(self) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.executor import ToolExecutor

        registry = _registry()
        executor = ToolExecutor(registry, role_tool_tags=["web"])
        results = await executor.execute([ToolCall(id="t1", name="bash", arguments={})])
        assert len(results) == 1
        assert results[0].success is False, "超出 role_tool_tags 的工具必须拒绝执行（fail-closed）"
        assert "role" in (results[0].error or "").lower()

    @pytest.mark.asyncio
    async def test_executor_allows_in_scope_tool(self) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.executor import ToolExecutor

        registry = _registry()
        executor = ToolExecutor(registry, role_tool_tags=["web"])
        results = await executor.execute([ToolCall(id="t1", name="web_search", arguments={})])
        assert results[0].success is True, f"标签内工具必须放行执行链 error={results[0].error}"

    @pytest.mark.asyncio
    async def test_executor_none_tags_no_gate(self) -> None:
        from bandi.core.message import ToolCall
        from bandi.tools.executor import ToolExecutor

        registry = _registry()
        executor = ToolExecutor(registry)
        results = await executor.execute([ToolCall(id="t1", name="read", arguments={})])
        assert results[0].success is True, (
            f"未配置 role_tool_tags 时不得启用门控 error={results[0].error}"
        )


class TestModeTagsIntersection:
    def test_plan_mode_and_tags_intersect(self) -> None:
        """mode 白名单与 role tags 叠加 = 交集（PLAN_MODE_TOOLS ∩ tags 工具集）。"""
        from bandi.agent.factory import filter_tools_by_role_tags
        from bandi.agent.mode_manager import ModeManager

        registry = _registry()
        mode_manager = ModeManager(initial_mode="plan")
        allowed = mode_manager.filter_tools([t.name for t in registry.list_tools()])
        filtered = filter_tools_by_role_tags(allowed, registry, ["code"])
        assert set(filtered) == set(), "plan 白名单 ∩ code 标签应为空集"
