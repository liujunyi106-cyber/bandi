from typing import Any

from bandi.tools.apply_patch import ApplyPatchTool
from bandi.tools.base import (
    BaseTool,
    ToolCategory,
    ToolDependency,
    ToolPermissionContext,
    ToolProgressEvent,
)
from bandi.tools.bash import BashTool
from bandi.tools.browser import BrowserTool
from bandi.tools.cache import ToolResultCache
from bandi.tools.canvas.canvas_publish import CanvasPublishTool
from bandi.tools.canvas.genui_guide import GetGenuiGuideTool
from bandi.tools.canvas.html_guide import GetHtmlCanvasGuideTool
from bandi.tools.canvas.ui_components import EmitUiTreeTool, ListUiComponentsTool
from bandi.tools.db.db_connection import DbConnectTool
from bandi.tools.db.db_execute import DbExecuteTool
from bandi.tools.db.db_inspect import DbInspectTool
from bandi.tools.dependency_checker import DependencyChecker
from bandi.tools.executor import ToolExecutor
from bandi.tools.file_edit import FileEditTool
from bandi.tools.file_ops import FileReadTool, FileWriteTool
from bandi.tools.git_ops import GitCommitTool, GitDiffTool, GitStatusTool
from bandi.tools.glob_tool import GlobTool
from bandi.tools.grep_tool import GrepTool
from bandi.tools.integration.configure_settings import ConfigureSettingsTool
from bandi.tools.integration.speech_to_text import SpeechToTextTool
from bandi.tools.list_dir import ListDirTool
from bandi.tools.mcp_broker import MCPBroker
from bandi.tools.mcp_client import (
    MCPClient,
    MCPError,
    StdioTransport,
    StreamableHTTPTransport,
    register_mcp_tools,
)
from bandi.tools.middleware import (
    CircuitBreakerMiddleware,
    PermissionMiddleware,
    RateLimitMiddleware,
    RetryMiddleware,
    SlidingWindowRateLimiter,
    TelemetryMiddleware,
)
from bandi.tools.permissions_pipeline import PermissionsPipeline, PermissionVerdict
from bandi.tools.pipeline import ToolContext, ToolMiddleware, ToolPipeline
from bandi.tools.python_exec import PythonExecTool
from bandi.tools.registry import ToolRegistry
from bandi.tools.todo import TodoReadTool, TodoWriteTool
from bandi.tools.user_input import UserInputTool
from bandi.tools.web.image_download import WebImageDownloadTool
from bandi.tools.web.image_search_tool import WebImageSearchTool
from bandi.tools.web.login import WebLoginTool
from bandi.tools.web.scraper import WebScraperTool
from bandi.tools.web_fetch import WebFetchTool
from bandi.tools.web_search import WebSearchTool

_LAZY_DATA_TOOLS = {
    "DataAggregateTool": "bandi.tools.data.data_aggregate",
    "DataCleanTool": "bandi.tools.data.data_clean",
    "DataMergeTool": "bandi.tools.data.data_merge",
    "DataTransformTool": "bandi.tools.data.data_transform",
    "DataValidateTool": "bandi.tools.data.data_validate",
}


def __getattr__(name: str) -> Any:
    # 功能：数据工具惰性导入——pandas 不在 API 启动期加载，避免拖慢内核启动
    module_name = _LAZY_DATA_TOOLS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "ApplyPatchTool",
    "MCPBroker",
    "MCPClient",
    "MCPError",
    "register_mcp_tools",
    "StdioTransport",
    "StreamableHTTPTransport",
    "BaseTool",
    "BashTool",
    "BrowserTool",
    "CanvasPublishTool",
    "CircuitBreakerMiddleware",
    "DataAggregateTool",
    "DataCleanTool",
    "DataMergeTool",
    "DataTransformTool",
    "DataValidateTool",
    "DbConnectTool",
    "DbExecuteTool",
    "DbInspectTool",
    "DependencyChecker",
    "EmitUiTreeTool",
    "FileEditTool",
    "FileReadTool",
    "FileWriteTool",
    "GetGenuiGuideTool",
    "GetHtmlCanvasGuideTool",
    "GitCommitTool",
    "GitDiffTool",
    "GitStatusTool",
    "GlobTool",
    "GrepTool",
    "ConfigureSettingsTool",
    "ListDirTool",
    "ListUiComponentsTool",
    "PermissionMiddleware",
    "PermissionVerdict",
    "PermissionsPipeline",
    "PythonExecTool",
    "RateLimitMiddleware",
    "RetryMiddleware",
    "SlidingWindowRateLimiter",
    "TelemetryMiddleware",
    "TodoReadTool",
    "TodoWriteTool",
    "ToolCategory",
    "ToolContext",
    "ToolDependency",
    "ToolPermissionContext",
    "ToolProgressEvent",
    "ToolResultCache",
    "ToolExecutor",
    "ToolMiddleware",
    "ToolPipeline",
    "ToolRegistry",
    "UserInputTool",
    "WebFetchTool",
    "WebImageDownloadTool",
    "WebImageSearchTool",
    "WebLoginTool",
    "WebScraperTool",
    "WebSearchTool",
    "SpeechToTextTool",
]
