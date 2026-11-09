# 功能：契约——加固② MCP 远端工具敏感路径本地剔除（客户端把关）。
# 钉行为：mcp_remote 标记的远端工具，其参数中路径形态值经 project_root 解析后命中
# 敏感路径（密钥/凭据等）→ fail-closed 拒绝且工具不得真正执行；
# 正常参数与普通文本（URL 等）放行；本地工具不受本层影响（破坏者）；
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool, ToolCategory
from bandi.tools.executor import ToolExecutor
from bandi.tools.mcp_client import register_mcp_tools
from bandi.tools.registry import ToolRegistry

# 敏感路径夹具：真实凭据路径形态（平台无关，供 file:// 与盘符写法派生）
_SSH_KEY = Path.home() / ".ssh" / "id_rsa"


class _RemoteProbeTool(BaseTool):
    name = "remote_probe"
    description = "远端探针"
    category = ToolCategory.INTEGRATION
    mcp_remote = True
    execute_count = 0

    async def execute(self, params, context=None) -> str:  # type: ignore[override]
        type(self).execute_count += 1
        return "executed"


class _LocalProbeTool(BaseTool):
    name = "local_probe"
    description = "本地探针"
    category = ToolCategory.UTIL
    execute_count = 0

    async def execute(self, params, context=None) -> str:  # type: ignore[override]
        type(self).execute_count += 1
        return "executed"


def _run_case(tool: BaseTool, arguments: dict[str, Any], project_root: str | None) -> Any:
    registry = ToolRegistry()
    registry.register_instance(tool)
    executor = ToolExecutor(registry=registry)
    if project_root is not None:
        executor.bind_session_meta({"session_id": "s-g2", "project_root": project_root})
    (result,) = asyncio.run(
        executor.execute([ToolCall(id="c1", name=tool.name, arguments=arguments)])
    )
    return result


# 远端工具**非标准键名**参数（filename，通用路径参数元组未覆盖）
# 含绝对敏感路径 → 远端扫描层拒绝执行且工具零调用（新层核心增值点）
def test_remote_tool_sensitive_path_under_nonstandard_key_is_denied(tmp_path: Path) -> None:
    _RemoteProbeTool.execute_count = 0
    result = _run_case(_RemoteProbeTool(), {"filename": "~/.ssh/id_rsa"}, str(tmp_path))

    assert result.success is False
    assert "sensitive" in (result.error or "").lower()
    assert _RemoteProbeTool.execute_count == 0, "拒绝路径不得真正执行远端工具"


# 远端工具相对路径参数经 project_root 解析后命中敏感目录 → 拒绝
def test_remote_tool_relative_traversal_into_sensitive_dir_is_denied(tmp_path: Path) -> None:
    _RemoteProbeTool.execute_count = 0
    result = _run_case(_RemoteProbeTool(), {"locator": "../../.ssh/id_rsa"}, str(tmp_path))

    assert result.success is False
    assert _RemoteProbeTool.execute_count == 0


# 远端工具正常参数放行（普通文本 / URL / 非敏感路径不误伤）
def test_remote_tool_normal_arguments_pass(tmp_path: Path) -> None:
    _RemoteProbeTool.execute_count = 0
    result = _run_case(
        _RemoteProbeTool(),
        {
            "query": "hello",
            "url": "https://example.com/api/v1",
            "filename": "docs/readme.md",
            "note": "请读取 README 并总结",
        },
        str(tmp_path),
    )

    assert result.success is True and result.output == "executed"
    assert _RemoteProbeTool.execute_count == 1


# 破坏者：本地工具不受本层影响——同一非标准键名敏感值，本地照常执行
# （通用层路径参数元组不含 filename；新增远端扫描层不得波及本地工具）
def test_local_tool_is_not_gated_by_remote_layer(tmp_path: Path) -> None:
    _LocalProbeTool.execute_count = 0
    result = _run_case(_LocalProbeTool(), {"filename": "~/.ssh/id_rsa"}, str(tmp_path))

    assert result.success is True and _LocalProbeTool.execute_count == 1


# 注册面：MCP 工具对象必须携带 mcp_remote 标记（执行层据此把关）
def test_registered_mcp_tool_carries_remote_marker() -> None:
    client = MagicMock()
    client.name = "srv"
    client.available = True
    client.list_tools = AsyncMock(
        return_value=[
            {"name": "read_file", "description": "读文件", "inputSchema": {"type": "object"}}
        ]
    )
    registry = ToolRegistry()

    asyncio.run(register_mcp_tools(registry, client))

    tool = registry.get("srv_read_file")
    assert tool is not None
    assert getattr(tool, "mcp_remote", False) is True, "MCP 注册产物必须带远端标记"


# 破坏者：file:// URI 形态不得绕过——原启发式遇 "://" 直接跳过，而远端 MCP
# server 多为本地进程，file:///C:/... 仍会按本地路径解析
def test_remote_tool_file_uri_sensitive_path_is_denied(tmp_path: Path) -> None:
    _RemoteProbeTool.execute_count = 0
    result = _run_case(_RemoteProbeTool(), {"target": _SSH_KEY.as_uri()}, str(tmp_path))

    assert result.success is False, "file:// 形态敏感路径必须拒绝"
    assert _RemoteProbeTool.execute_count == 0


def test_remote_tool_nested_sensitive_path_is_denied(tmp_path: Path) -> None:
    _RemoteProbeTool.execute_count = 0
    result = _run_case(
        _RemoteProbeTool(),
        {"options": {"targets": [{"locator": "~/.ssh/id_rsa"}]}},
        str(tmp_path),
    )

    assert result.success is False, "嵌套参数中的敏感路径必须拒绝"
    assert _RemoteProbeTool.execute_count == 0


# 破坏者：盘符双斜杠写法（C://x）不得被 "://" 误判为 URL scheme 而放过
def test_remote_tool_drive_letter_double_slash_is_denied(tmp_path: Path) -> None:
    _RemoteProbeTool.execute_count = 0
    result = _run_case(
        _RemoteProbeTool(),
        {"locator": str(_SSH_KEY).replace("\\", "//")},
        str(tmp_path),
    )

    assert result.success is False, "盘符双斜杠写法必须按本地路径把关"
    assert _RemoteProbeTool.execute_count == 0
