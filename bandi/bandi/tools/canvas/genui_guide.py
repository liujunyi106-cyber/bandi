# 功能：GetGenuiGuideTool — 按需注入生成 UI 布局/视觉指引（无副作用）
from __future__ import annotations

from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext

_GUIDE: dict[str, Any] = {
    "purpose": (
        "Ship polished, scannable generative UI: clear hierarchy, calm spacing, "
        "consistent variants — not noisy emoji decoration or flat walls of text."
    ),
    "node_shape": [
        "Envelope: call `emit_ui_tree` with a root node only; the server wraps it.",
        "Every node is `{kind, props, children}`. Component fields live under `props`.",
        "`kind` must match the catalog exactly — call `list_ui_components` first.",
        "`children` holds only nested node objects, never raw strings.",
    ],
    "workflow": [
        "1. Confirm GenUI is appropriate (component UIs, not plain Q&A).",
        "2. Call `list_ui_components` to verify every kind/prop you plan to use.",
        "3. Build the tree with a `DesignSurface` root and one `preset`.",
        "4. Use `Stack`/`Grid`/`Card` to group; avoid 10+ flat peer nodes.",
        "5. Avoid deep nesting beyond ~4 levels where a flatter Grid+Card reads better.",
    ],
    "layout": [
        "One primary focal point per block: a single `Heading` then supporting Text/Stat/Table.",
        "Prefer `List`+`ListItem` over multiple `Text` bullets.",
        "Use `Card` `padding` md (or lg for hero cards); align siblings.",
    ],
    "typography": [
        "Use `Heading` for titles, `Text` for body; use `size` sm and `color` muted for secondary lines.",
        "Maximum one `Heading` level 1 per tree.",
    ],
    "emoji_and_icons": [
        "Do not decorate headings or every list item with emoji.",
        "At most one decorative emoji per card/section unless the user asks for playful tone.",
    ],
    "semantics": [
        "Use `Badge`/`Stat` variants (success/warning/error/info) for meaning.",
        "Charts: pass clean `categories` and `series`; prefer `Chart` over screenshots.",
    ],
    "anti_patterns": [
        "Unknown `kind` strings (must match catalog).",
        "Node keys outside `kind`/`props`/`children`/`nodeId`.",
        "Emoji prefix on every line.",
        "10+ flat peer nodes with no Card/Stack/Grid grouping.",
        "Using Markdown as a dump when List/Table/Card would scan better.",
    ],
}


class GetGenuiGuideTool(BaseTool):
    name: str = "get_genui_guide"
    description: str = (
        "Return the generative UI authoring guide: node shape, workflow order, layout, "
        "typography, emoji/icon rules, semantic variants, and anti-patterns. Call before "
        "non-trivial `emit_ui_tree` payloads, then `list_ui_components`. Read-only, no side effects."
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
        return {"success": True, "guide": _GUIDE}
