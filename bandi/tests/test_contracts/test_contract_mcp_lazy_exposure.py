# 功能：契约测试——MCP 工具默认懒暴露，普通对话不携带 MCP schema
from __future__ import annotations

from bandi.core.config import BandiConfig
from bandi.tools.selection import select_relevant_schemas


def _schema(name: str, description: str = "") -> dict:
    return {"type": "function", "function": {"name": name, "description": description}}


MCP_NAMES = {
    "srv_github_search",
    "srv_slack_post",
    "srv_calendar_create",
    "srv_drive_upload",
}


def _mcp_schemas() -> list[dict]:
    return [
        _schema("srv_github_search", "search github issues and pull requests"),
        _schema("srv_slack_post", "post a message to slack"),
        _schema("srv_calendar_create", "create a calendar event"),
        _schema("srv_drive_upload", "upload a file to drive"),
    ]


def _names(schemas: list[dict]) -> set[str]:
    return {str(item["function"]["name"]) for item in schemas}


def test_default_config_enables_mcp_with_lazy_exposure() -> None:
    tools = BandiConfig().tools
    assert tools.mcp_autostart is True
    assert tools.mcp_lazy_exposure is True
    assert tools.mcp_max_exposed_tools == 5


def test_generic_chat_does_not_expose_mcp_schema() -> None:
    schemas = [_schema("read", "read a file"), *_mcp_schemas()]
    picked = select_relevant_schemas(
        schemas,
        "你好",
        mcp_tool_names=MCP_NAMES,
        mcp_lazy=True,
        mcp_max_tools=5,
    )
    assert not (_names(picked) & MCP_NAMES)


def test_business_query_without_mcp_intent_does_not_expose_mcp() -> None:
    schemas = [_schema("read", "read a file"), *_mcp_schemas()]
    picked = select_relevant_schemas(
        schemas,
        "帮我看下这个文件",
        mcp_tool_names=MCP_NAMES,
        mcp_lazy=True,
        mcp_max_tools=5,
    )
    assert not (_names(picked) & MCP_NAMES)


def test_mcp_keyword_selects_relevant_tool_only() -> None:
    schemas = [_schema("read", "read a file"), *_mcp_schemas()]
    picked = select_relevant_schemas(
        schemas,
        "查 GitHub issue",
        mcp_tool_names=MCP_NAMES,
        mcp_lazy=True,
        mcp_max_tools=5,
    )
    names = _names(picked)
    assert "srv_github_search" in names
    assert "srv_slack_post" not in names


def test_explicit_mcp_query_is_capped() -> None:
    schemas = [_schema("read", "read a file"), *_mcp_schemas()]
    picked = select_relevant_schemas(
        schemas,
        "使用 MCP 帮我处理一下",
        mcp_tool_names=MCP_NAMES,
        mcp_lazy=True,
        mcp_max_tools=2,
    )
    assert len(_names(picked) & MCP_NAMES) == 2


def test_explicit_tool_name_is_selected() -> None:
    schemas = [_schema("read", "read a file"), *_mcp_schemas()]
    picked = select_relevant_schemas(
        schemas,
        "srv_slack_post 发消息",
        mcp_tool_names=MCP_NAMES,
        mcp_lazy=True,
        mcp_max_tools=5,
    )
    assert "srv_slack_post" in _names(picked)


def test_eager_mode_keeps_existing_behavior() -> None:
    schemas = [_schema("read", "read a file"), *_mcp_schemas()]
    picked = select_relevant_schemas(
        schemas,
        "处理一下这个任务",
        mcp_tool_names=MCP_NAMES,
        mcp_lazy=False,
        mcp_max_tools=2,
    )
    assert MCP_NAMES <= _names(picked)
