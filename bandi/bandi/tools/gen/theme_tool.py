# 功能：ThemeDesignerTool — 品牌色→主题推导 + WCAG 对比度 lint + 自定义主题 store
from __future__ import annotations

from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.gen.store import JsonStore, derive_theme_payload, lint_theme, validate_store_name

_ACTIONS = ("create", "save", "list", "get", "delete")


class ThemeDesignerTool(FsTool):
    name: str = "theme_designer"
    description: str = (
        "Design and manage custom visual themes for document/slides output. "
        "`create` derives a complete contrast-checked theme from a brand seed "
        "(primary color + light/dark mode + optional accent and fonts); `save` "
        "stores an explicit theme payload; `list`/`get`/`delete` manage the "
        "store. `lint` reports WCAG contrast warnings for text/background/surface "
        "pairs, so runs on every create/save."
    )
    category: ToolCategory = ToolCategory.GEN
    feature_group: str = "gen"
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    risk_level: str = "low"
    is_concurrency_safe: bool = False
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "create/save/list/get/delete.",
            },
            "name": {
                "type": "string",
                "description": "Theme name (lowercase letters/digits/-/_). Required except list.",
            },
            "kind": {
                "type": "string",
                "enum": ["document", "deck"],
                "description": "What the theme targets. Default document.",
            },
            "primary": {"type": "string", "description": "Brand primary color '#RRGGBB' (create)."},
            "accent": {
                "type": "string",
                "description": "Accent color; derived from primary when omitted.",
            },
            "mode": {
                "type": "string",
                "enum": ["light", "dark"],
                "description": "Light or dark scheme (create).",
            },
            "heading_font": {"type": "string", "description": "Heading font name (create)."},
            "body_font": {"type": "string", "description": "Body font name (create)."},
            "east_asia_font": {"type": "string", "description": "East-Asian font (create)."},
            "overrides": {
                "type": "object",
                "additionalProperties": {},
                "description": "Partial theme payload deep-merged over the derived theme (create).",
            },
            "payload": {
                "type": "object",
                "additionalProperties": {},
                "description": "Full/partial theme payload for action=save.",
            },
            "lint": {
                "type": "boolean",
                "description": "Run WCAG contrast lint (default true).",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Return derived payload + lint without persisting.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, store: JsonStore | None = None) -> None:
        self._store = store or JsonStore()

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        action = str(params.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return {"success": False, "error": f"unknown action: {action} (use {_ACTIONS})"}
        kind = params.get("kind") or "document"
        if action == "list":
            stored = self._store.list("themes")
            for item in stored:
                item.pop("kind", None)
            return {"success": True, "themes": stored}
        name = params.get("name")
        if not name:
            return {"success": False, "error": f"`name` is required for action={action}"}
        try:
            validate_store_name(str(name))
            if action == "create":
                return await self._create(str(name), kind, params)
            if action == "save":
                return await self._save(str(name), kind, params)
            if action == "delete":
                removed = self._store.delete("themes", str(name))
                return {"success": True, "deleted": removed, "name": name}
            payload = self._store.load("themes", str(name))
            if payload is None:
                return {"success": False, "error": f"theme not found: {name!r}"}
            payload["name"] = str(name)
            result: dict[str, Any] = {"success": True, "theme": payload}
            if params.get("lint", True):
                result["lint_warnings"] = lint_theme(payload)
            return result
        except ValueError as e:
            return {"success": False, "error": str(e)}

    async def _create(self, name: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        primary = params.get("primary")
        if not primary:
            raise ValueError("`primary` (brand color '#RRGGBB') is required for create")
        payload = derive_theme_payload(
            kind=kind,
            primary=str(primary),
            accent=params.get("accent"),
            mode=params.get("mode", "light"),
            heading_font=params.get("heading_font"),
            body_font=params.get("body_font"),
            east_asia_font=params.get("east_asia_font"),
        )
        overrides = params.get("overrides")
        if isinstance(overrides, dict) and overrides:
            _deep_merge(payload, overrides)
        return self._finish(name, kind, payload, params)

    async def _save(self, name: str, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = params.get("payload")
        if not isinstance(payload, dict) or not payload:
            raise ValueError("`payload` (theme object) is required for save")
        return self._finish(name, kind, dict(payload), params)

    def _finish(
        self, name: str, kind: str, payload: dict[str, Any], params: dict[str, Any]
    ) -> dict[str, Any]:
        payload.setdefault("kind", kind)
        payload.setdefault("name", name)
        lint_warnings = lint_theme(payload) if params.get("lint", True) else []
        result: dict[str, Any] = {
            "success": True,
            "name": name,
            "kind": kind,
            "payload": payload,
            "lint_warnings": lint_warnings,
        }
        if params.get("dry_run"):
            result["saved"] = False
            return result
        self._store.save("themes", name, payload, overwrite=True)
        result["saved"] = True
        result["usage"] = f"pass this theme name to document/slides output styling: name='{name}'"
        return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
