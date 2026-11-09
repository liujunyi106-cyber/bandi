# 功能：ui_components — 生成 UI 组件目录 + 组件树校验/发射工具
from __future__ import annotations

import json
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

_COMPONENT_CATALOG: dict[str, dict[str, Any]] = {
    "DesignSurface": {"props": {"preset": "minimal|editorial|card|slide|poster|geek"}},
    "Stack": {"props": {"direction": "vertical|horizontal", "gap": "sm|md|lg"}},
    "Grid": {"props": {"columns": "int", "gap": "sm|md|lg"}},
    "Card": {"props": {"padding": "sm|md|lg", "variant": "default|outline|elevated"}},
    "Heading": {"props": {"level": "1..6", "text": "str"}},
    "Text": {"props": {"text": "str", "size": "sm|base|lg", "color": "default|muted"}},
    "Badge": {"props": {"text": "str", "variant": "default|success|warning|error|info"}},
    "Stat": {"props": {"label": "str", "value": "str|num", "delta": "str"}},
    "List": {"props": {}},
    "ListItem": {"props": {"text": "str"}},
    "Table": {"props": {"headers": "[str]", "rows": "[[str]]"}},
    "Markdown": {"props": {"content": "str"}},
    "Image": {"props": {"src": "str", "alt": "str", "fit": "cover|contain"}},
    "Spacer": {"props": {"size": "sm|md|lg"}},
    "Chart": {"props": {"type": "bar|line|pie", "categories": "[str]", "series": "[{name,data}]"}},
    "AspectBox": {"props": {"ratio": "str", "children": "nodes"}},
    "HtmlFrame": {"props": {"html": "str", "height": "str"}},
    "LinkButton": {"props": {"text": "str", "href": "str"}},
}

_MAX_DEPTH = 12
_MAX_NODES = 200


def validate_ui_tree(tree: dict[str, Any]) -> dict[str, Any]:
    # 功能：校验组件树（节点形状 + kind 目录 + 深度/节点上限），返回浅拷贝规范化后的树
    count = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if count > _MAX_NODES:
            raise ValueError(f"tree exceeds max node count {_MAX_NODES}")
        if depth > _MAX_DEPTH:
            raise ValueError(f"tree exceeds max depth {_MAX_DEPTH}")
        if not isinstance(node, dict):
            raise ValueError("tree nodes must be objects")
        kind = node.get("kind")
        if not isinstance(kind, str) or kind not in _COMPONENT_CATALOG:
            raise ValueError(f"unknown component kind: {kind!r}")
        banned = set(node.keys()) - {"kind", "props", "children", "nodeId"}
        if banned:
            raise ValueError(f"unexpected node keys: {sorted(banned)}")
        props = node.get("props")
        if props is None:
            node["props"] = {}
        elif not isinstance(props, dict):
            raise ValueError("node props must be an object")
        children = node.get("children")
        if children is not None:
            if not isinstance(children, list):
                raise ValueError("node children must be an array")
            for child in children:
                walk(child, depth + 1)

    walk(tree, 0)
    return dict(tree)


def emit_ui_tree(tree: dict[str, Any]) -> dict[str, Any]:
    # 功能：发射组件树——校验后返回（渲染由前端订阅处理，本工具无副作用）
    normalized = validate_ui_tree(tree)
    return {"tree": normalized}


class ListUiComponentsTool(BaseTool):
    name: str = "list_ui_components"
    description: str = (
        "Return the generative UI component catalog (kinds + prop hints). Call this "
        "before authoring any non-trivial `emit_ui_tree` payload so every planned "
        "kind and prop is grounded in the shipped catalog. Read-only, no side effects."
    )
    category: ToolCategory = ToolCategory.CANVAS
    feature_group: str = "canvas"
    capability_tags: list[str] = []
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        return {
            "success": True,
            "node_shape": '{"kind":"...", "props":{...}, "children":[...]}',
            "rules": [
                "Every component prop goes inside `props`.",
                "`children` is only for nested nodes, never strings.",
                "Use `nodeId` only if you need a stable id; otherwise omit.",
            ],
            "components": _COMPONENT_CATALOG,
        }


class EmitUiTreeTool(BaseTool):
    name: str = "emit_ui_tree"
    description: str = (
        "Emit a validated generative UI tree that renders inline in the chat as a "
        "card/dashboard/table/chart layout. Use for component-style UIs (charts, KPI "
        "dashboards, cards, slides). For a full standalone HTML page use `canvas_publish` "
        "instead. Schema: every node is `{kind, props, children}`; component fields stay "
        "inside `props`; `children` holds only nested nodes. Call `get_genui_guide` then "
        "`list_ui_components` before non-trivial trees."
    )
    category: ToolCategory = ToolCategory.CANVAS
    feature_group: str = "canvas"
    capability_tags: list[str] = []
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["tree"],
        "additionalProperties": False,
        "properties": {
            "tree": {
                "type": ["object", "string"],
                "description": (
                    "Root node (DesignSurface) of the component tree. "
                    "Accepts an object, or a JSON-encoded string (auto-parsed)."
                ),
            }
        },
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        tree = params.get("tree")
        # 模型常把树序列化成 JSON 字符串提交——宽容解析，避免整轮工具失败
        if isinstance(tree, str):
            try:
                tree = json.loads(tree)
            except json.JSONDecodeError as exc:
                return {"success": False, "error": f"tree 字符串不是合法 JSON: {exc}"}
        if not isinstance(tree, dict):
            return {"success": False, "error": "tree must be an object"}
        try:
            normalized = validate_ui_tree(tree)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "tree": normalized}
