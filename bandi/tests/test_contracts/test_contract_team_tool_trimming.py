# 功能：契约测试——团队员工走角色 tool_tags 裁剪；无派发面剔除管理者工具
from __future__ import annotations

from typing import Any

from bandi.agent.factory import filter_tools_by_role_tags
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.registry import ToolRegistry


class _DocTool(BaseTool):
    name: str = "read_document"
    description: str = "读文档"
    category: ToolCategory = ToolCategory.DOC

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "doc"


class _CodeTool(BaseTool):
    name: str = "bash"
    description: str = "命令"
    category: ToolCategory = ToolCategory.CODE

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "ok"


class _BoardTool(BaseTool):
    name: str = "manager_board_read"
    description: str = "看子卡"
    category: ToolCategory = ToolCategory.WORKFLOW

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "{}"


class _CollabTool(BaseTool):
    name: str = "send_dm"
    description: str = "发消息"
    category: ToolCategory = ToolCategory.WORKFLOW

    async def execute(self, params: dict[str, Any], context: Any = None) -> str:
        return "ok"


def _registry() -> ToolRegistry:
    reg = ToolRegistry()
    for tool in (_DocTool(), _CodeTool(), _BoardTool(), _CollabTool()):
        reg.register_instance(tool)
    return reg


def _names() -> list[str]:
    return ["read_document", "bash", "manager_board_read", "send_dm"]


class TestTeamToolTrimming:
    def test_role_tags_trim_team_tool_surface(self) -> None:
        kept = filter_tools_by_role_tags(_names(), _registry(), ["doc"], manager_capable=True)
        assert "read_document" in kept
        assert "bash" not in kept, "未配置 code 类别的岗位不得拿到 bash"

    def test_collab_tools_always_kept(self) -> None:
        kept = filter_tools_by_role_tags(_names(), _registry(), ["doc"], manager_capable=True)
        assert "send_dm" in kept, "协作工具是组织结构性权利"

    def test_non_manager_drops_manager_only_tools(self) -> None:
        kept = filter_tools_by_role_tags(_names(), _registry(), ["doc"], manager_capable=False)
        assert "manager_board_read" not in kept, "无派发面的员工卡不得拿到管理者工具"
        assert "send_dm" in kept

    def test_non_manager_drops_board_tools_even_without_tags(self) -> None:
        """换团队/未配置角色标签时同样生效——管理者工具裁剪与团队无关。"""
        kept = filter_tools_by_role_tags(_names(), _registry(), None, manager_capable=False)
        assert "manager_board_read" not in kept
        assert "read_document" in kept and "send_dm" in kept

    def test_manager_keeps_board_tools(self) -> None:
        kept = filter_tools_by_role_tags(_names(), _registry(), ["doc"], manager_capable=True)
        assert "manager_board_read" in kept, "有派发面的管理者保留 board 工具"

    def test_empty_tags_still_fail_closed(self) -> None:
        assert filter_tools_by_role_tags(_names(), _registry(), []) == [], (
            "空名单 = 显式全禁（权限路径语义不变）"
        )

    def test_unconfigured_tags_no_filter(self) -> None:
        assert filter_tools_by_role_tags(_names(), _registry(), None) == _names()
