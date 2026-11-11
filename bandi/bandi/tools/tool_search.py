# 功能：ToolSearchTool——运行时按关键词/select 检索内部工具与 MCP 外部工具
from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.registry import ToolRegistry

_DEFAULT_MAX_RESULTS = 5


def _parse_tool_name(name: str) -> tuple[list[str], str]:
    # 功能：工具名分词——MCP 名（filesystem_read_file → [filesystem, read, file]）
    parts = name.replace("_", " ").lower().split()
    return parts, " ".join(parts)


def _keyword_search(
    tools: list[BaseTool],
    query: str,
    max_results: int,
) -> list[str]:
    # 功能：关键词评分检索——
    # 名称词素精确命中权重最高，其次名称子串/描述/参数关键词 word-boundary
    query_lower = query.lower().strip()
    if not query_lower:
        return []
    exact = next((t.name for t in tools if t.name.lower() == query_lower), None)
    if exact:
        return [exact]

    terms = [t for t in query_lower.split() if t]
    scored: list[tuple[int, str]] = []
    for tool in tools:
        parts, full = _parse_tool_name(tool.name)
        desc_norm = (tool.description or "").lower()

        def _has_param_word(word: str) -> bool:
            # 功能：参数 schema 关键词命中（property 名/描述）
            props = (tool.parameters or {}).get("properties", {})
            for pname, pinfo in props.items():
                if word in str(pname).lower() or word in str(pinfo.get("description", "")).lower():
                    return True
            return False

        score = 0
        for term in terms:
            if term in parts:
                score += 12
            elif any(term in p for p in parts):
                score += 6
            elif term in full and score == 0:
                score += 3
            if term in desc_norm:
                score += 2
            if _has_param_word(term):
                score += 1
        if score > 0:
            scored.append((score, tool.name))

    scored.sort(key=lambda x: (-x[0], x[1]))
    return [name for _, name in scored[:max_results]]


class ToolSearchTool(BaseTool):
    name: str = "tool_search"
    description: str = (
        "Search available tools (internal and MCP-integrated external tools) by "
        'keyword or select: to discover tool names. Query forms: "read file" '
        'keyword search, "select:tool1,tool2" exact names. Matched tool schemas '
        "are loaded into the next turn. Returns matched tool names."
    )
    category: ToolCategory = ToolCategory.UTIL
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    feature_group: str | None = None
    capability_tags: list[str] = []
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": 'Keyword query or "select:name1,name2" for exact selection',
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of matches (default 5)",
            },
        },
        "required": ["query"],
    }

    def __init__(self, registry: ToolRegistry) -> None:
        super().__init__()
        self._registry = registry

    async def execute(
        self,
        params: dict[str, Any],
        context: ToolPermissionContext | None = None,
    ) -> str | dict[str, Any]:
        query = str(params.get("query", ""))
        max_results = int(params.get("max_results", _DEFAULT_MAX_RESULTS))
        if max_results <= 0:
            max_results = _DEFAULT_MAX_RESULTS
        return self._registry.search(query, max_results=max_results, scope="all")
