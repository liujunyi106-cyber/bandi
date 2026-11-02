# 功能：ToolSearchTool 契约——search 检索面=MCP 工具、评分排序、select 直选、disabled 不泄
# disabled 工具 fail-closed 不泄漏、评分权重名称>描述>参数、select 直选透传。
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


class TestSearchScopeMcpOnly:
    def test_search_only_returns_mcp_tools(self) -> None:
        """检索面只含 MCP 工具：内置 read 不作为候选，也不会出现在结果。"""
        r = _make_registry()
        result = r.search("read")
        assert result["matches"] == ["filesystem_read_file"]
        assert result["total_deferred_tools"] == 3

    def test_total_deferred_counts_only_mcp(self) -> None:
        """total_deferred_tools = 检索面（MCP）计数，与内置工具数无关。"""
        r = _make_registry()
        assert r.search("nothing").get("total_deferred_tools") == 3

        r_no_inner = ToolRegistry()
        r_no_inner.register_instance(_McpReadTool())
        assert r_no_inner.search("x").get("total_deferred_tools") == 1

    def test_category_filter_mcp_list(self) -> None:
        """category filter 仅返回该类别的 MCP 工具（不炸、不透传内置）。"""
        r = _make_registry()
        result = r.search("select:fs_write", category=ToolCategory.INTEGRATION.value)
        assert result["matches"] == ["fs_write"]
        result_other = r.search("select:fs_write", category=ToolCategory.UTIL.value)
        assert result_other["matches"] == []
        assert result_other["total_deferred_tools"] == 3

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
        r = _make_registry()
        result = r.search("select:nonexistent,read")
        assert result["matches"] == []


class TestSearchFailClosed:
    def test_disabled_mcp_tool_not_returned(self) -> None:
        """enabled=False 的 MCP 工具不泄漏（与 get/list_schemas fail-closed 同口径）。"""
        r = _make_registry()
        r._tools["fs_list"].enabled = False
        result = r.search("list")
        assert "fs_list" not in result["matches"]
        assert result["total_deferred_tools"] == 2

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
        assert result["matches"] == ["filesystem_read_file"]


class TestFactoryWiring:
    def test_factory_registers_tool_search(self) -> None:
        """生产装配 build_tool_registry 注册 tool_search（生产调用点）。"""
        from bandi.agent.factory import build_tool_registry

        registry = build_tool_registry()
        tool = registry.get("tool_search")
        assert tool is not None
        assert tool.is_read_only is True
        assert registry.search("select:tool_search")["query"] == "select:tool_search"
