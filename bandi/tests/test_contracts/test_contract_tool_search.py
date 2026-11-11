# 功能：ToolSearchTool 契约——search() 默认检索内部 + MCP 工具、评分排序、select 直选、disabled 不泄
# 契约钉「行为结果/最终产物」：默认检索面覆盖内部工具与 MCP、scope=mcp 保持外部专用、
# disabled 工具 fail-closed 不泄漏、评分权重名称>描述>参数、select 直选透传、命中后按授权扩面。
from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.registry import ToolRegistry
from bandi.tools.tool_search import ToolSearchTool


class _InnerTool(BaseTool):
    name = "read"
    description = "read files from disk"
    category = ToolCategory.CODE

    async def execute(self, params, context=None):
        return None


class _McpReadTool(BaseTool):
    name = "filesystem_read_file"
    description = ""
    category = ToolCategory.INTEGRATION
    provenance = "mcp:filesystem"

    async def execute(self, params, context=None):
        return None


class _McpListTool(BaseTool):
    name = "fs_list"
    description = "list files in a directory"
    category = ToolCategory.INTEGRATION
    provenance = "mcp:fs"

    async def execute(self, params, context=None):
        return None


class _McpWriteTool(BaseTool):
    name = "fs_write"
    description = ""
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    }
    category = ToolCategory.INTEGRATION
    provenance = "mcp:fs"

    async def execute(self, params, context=None):
        return None


def _make_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register_instance(_InnerTool())
    r.register_instance(_McpReadTool())
    r.register_instance(_McpListTool())
    r.register_instance(_McpWriteTool())
    return r


class TestSearchScope:
    def test_web_search_is_core_when_pruning(self) -> None:
        """联网入口不被裁剪：超量工具面仍保留 web_search。"""
        from bandi.tools.selection import select_relevant_schemas

        def _schema(name: str) -> dict[str, Any]:
            return {"type": "function", "function": {"name": name, "description": ""}}

        schemas = [_schema("web_search"), _schema("tool_search")]
        schemas.extend(_schema(f"noise_{i}") for i in range(60))
        picked = select_relevant_schemas(schemas, "分析竞品")
        assert "web_search" in {s["function"]["name"] for s in picked}

    def test_search_returns_internal_tool_by_exact_name(self) -> None:
        """默认检索面覆盖内部工具：select/关键词可直接命中 read。"""
        r = _make_registry()
        assert r.search("read")["matches"] == ["read"]

    def test_search_scope_mcp_only(self) -> None:
        """显式 scope=mcp 时保持外部工具专用检索。"""
        r = _make_registry()
        result = r.search("read", scope="mcp")
        assert result["matches"] == ["filesystem_read_file"]
        assert result["total_deferred_tools"] == 3

    def test_total_counts_internal_and_mcp(self) -> None:
        """默认检索面统计内部 + MCP；total_deferred = 本轮候选总数。"""
        r = _make_registry()
        result = r.search("nothing")
        assert result["total_mcp_tools"] == 3
        assert result["total_internal_tools"] == 1
        assert result["total_deferred_tools"] == 4
        assert result["total_tools"] == 4

    def test_category_filter_all_scope(self) -> None:
        """category 过滤在默认全量面生效；MCP-only 仍可用 scope=mcp。"""
        r = _make_registry()
        result = r.search("select:fs_write", category=ToolCategory.INTEGRATION.value)
        assert result["matches"] == ["fs_write"]
        result_other = r.search("select:fs_write", category=ToolCategory.UTIL.value)
        assert result_other["matches"] == []
        assert result_other["total_deferred_tools"] == 4

    def test_capability_filter_tags_list(self) -> None:
        """capability filter 基于 capability_tags 过滤（标签非空工具不炸、正确返回）。"""
        read = _McpReadTool()
        read.capability_tags = ["file_read"]
        write = _McpWriteTool()
        write.capability_tags = ["file_write"]
        r2 = ToolRegistry()
        r2.register_instance(read)
        r2.register_instance(write)
        result = r2.search("select:fs_write", capability="file_write")
        assert result["matches"] == ["fs_write"]
        assert result["total_deferred_tools"] == 2

    def test_capability_filter_excludes_non_matching(self) -> None:
        """capability 不匹配的工具被过滤掉（select 直选同样受过滤约束）。"""
        read = _McpReadTool()
        read.capability_tags = ["file_read"]
        write = _McpWriteTool()
        write.capability_tags = ["file_write"]
        r2 = ToolRegistry()
        r2.register_instance(read)
        r2.register_instance(write)
        result = r2.search("select:fs_write", capability="file_read")
        assert result["matches"] == []
        assert result["total_deferred_tools"] == 2


class TestSearchScoring:
    def test_rank_name_match_above_description_match(self) -> None:
        """同名命中优先于描述命中：query 命中名称词素排在描述命中前。"""
        r = _make_registry()
        result = r.search("list")
        # filesystem_read_file 名称无 list → 描述空不命中；fs_list 名称命中
        assert result["matches"][0] == "fs_list"

    def test_parameters_keyword_matches(self) -> None:
        """参数 schema 关键词也参与评分：描述为空但参数含 path/content 仍命中。"""
        r = _make_registry()
        result = r.search("content")
        assert "fs_write" in result["matches"]


class TestSearchSelect:
    def test_select_prefix_multi_select(self) -> None:
        """select: 逗号分隔多选——按名精确返回，缺失名不抛、不回退。"""
        r = _make_registry()
        result = r.search("select:filesystem_read_file,fs_write")
        assert set(result["matches"]) == {"filesystem_read_file", "fs_write"}

    def test_select_unknown_name_returns_empty(self) -> None:
        """默认全量面：不存在的名字不返回；已知内部工具仍可按名选中。"""
        r = _make_registry()
        result = r.search("select:nonexistent,read")
        assert result["matches"] == ["read"]
        assert r.search("select:nonexistent,read", scope="mcp")["matches"] == []


class TestSearchFailClosed:
    def test_disabled_mcp_tool_not_returned(self) -> None:
        """enabled=False 的 MCP 工具不泄漏（与 get/list_schemas fail-closed 同口径）。"""
        r = _make_registry()
        r._tools["fs_list"].enabled = False
        result = r.search("list")
        assert "fs_list" not in result["matches"]
        assert result["total_deferred_tools"] == 3

    def test_feature_gate_off_group_hidden_from_search(self) -> None:
        """联动：feature gate 关闭整组的 MCP 工具不进检索面（fail-closed）。"""
        r = ToolRegistry(feature_flags={"mcp_fs": False})
        r.register_instance(_McpReadTool())
        gated = _McpListTool()
        gated.feature_group = "mcp_fs"
        r.register_instance(gated)
        r.register_instance(_McpWriteTool())
        result = r.search("list")
        assert "fs_list" not in result["matches"]
        assert result["total_deferred_tools"] == 2

    def test_empty_query_no_crash(self) -> None:
        r = _make_registry()
        assert r.search("")["matches"] == []
        assert r.search("   ")["matches"] == []


class TestMaxResults:
    def test_max_results_limits(self) -> None:
        """max_results 截断返回数（默认 5，可指定更小）。"""
        r = _make_registry()
        result = r.search("fs", max_results=2)
        assert len(result["matches"]) <= 2
        assert result["query"] == "fs"


class TestToolSearchToolWiring:
    @staticmethod
    def _run(query: str, **extra: Any) -> dict[str, Any]:
        import asyncio

        r = _make_registry()
        tool = ToolSearchTool(registry=r)
        return asyncio.run(tool.execute({"query": query, **extra}))

    def test_execute_delegates_to_registry_search(self) -> None:
        """ToolSearchTool.execute → registry.search 参数透传（接线完整性）。"""
        result = self._run("select:fs_write", max_results=5)
        assert result["matches"] == ["fs_write"]
        assert result["query"] == "select:fs_write"

    def test_execute_default_max_results(self) -> None:
        result = self._run("read")
        assert isinstance(result, dict)
        assert result["matches"] == ["read"]


class TestDynamicExpansion:
    def test_search_result_injects_only_authorized_schemas(self) -> None:
        """tool_search 命中后下一轮只注入当前角色已授权的 schema。"""
        import json
        from types import SimpleNamespace

        from bandi.agent.states import _expand_tools_from_search

        r = _make_registry()
        ctx = SimpleNamespace(
            tool_executor=SimpleNamespace(_registry=r),
            meta={"allowed_tool_names": ["read"]},
            active_tools=[],
        )
        result = SimpleNamespace(
            tool_name="tool_search",
            success=True,
            output=json.dumps({"matches": ["read", "fs_write"]}),
        )
        _expand_tools_from_search(ctx, [result])
        names = {schema["function"]["name"] for schema in ctx.active_tools}
        assert names == {"read"}


class TestFactoryWiring:
    def test_factory_registers_tool_search(self) -> None:
        """生产装配 build_tool_registry 注册 tool_search（生产调用点）。"""
        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry()
        tool = registry.get("tool_search")
        assert tool is not None
        assert tool.is_read_only is True
        assert registry.search("select:tool_search")["query"] == "select:tool_search"

    def test_factory_search_can_find_internal_web_search(self) -> None:
        """生产注册表按关键词能检回内部 web_search，而非只有 MCP 工具。"""
        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry()
        result = registry.search("web_search")
        assert "web_search" in result["matches"]
