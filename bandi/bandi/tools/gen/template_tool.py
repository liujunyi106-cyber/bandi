# 功能：DocumentTemplateTool — 可复用文档/幻灯片模板管理（save/list/get/delete/preview/generate）
from __future__ import annotations

from typing import Any

from jinja2 import Environment, Undefined

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.gen.store import JsonStore

_ACTIONS = ("save", "list", "get", "delete", "preview", "generate")
_KINDS = ("document", "deck")


class DocumentTemplateTool(FsTool):
    name: str = "document_template"
    description: str = (
        "Manage reusable document/deck templates. `save` stores a template "
        "(markdown `content` for document kind, `slides` objects for deck kind) "
        "with Jinja2 `{{ variable }}` placeholders and declared `variables`; "
        "`generate` instantiates it with `values` and renders through "
        "document_generate / slides_generate to `output_path`; `preview` returns "
        "the rendered markdown/slides without writing; `list`/`get`/`delete` "
        "manage the store. Templates make recurring reports and decks repeatable."
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
                "description": "save/list/get/delete/preview/generate.",
            },
            "name": {
                "type": "string",
                "description": "Template name (lowercase letters/digits/-/_). Required except list.",
            },
            "kind": {
                "type": "string",
                "enum": list(_KINDS),
                "description": "document (markdown content) or deck (slides). Default document.",
            },
            "description": {"type": "string", "description": "One-line description (save)."},
            "content": {
                "type": "string",
                "description": "save (document): markdown body with Jinja2 placeholders.",
            },
            "slides": {
                "type": "array",
                "items": {"type": "object"},
                "description": "save (deck): slide objects with Jinja2 placeholders.",
            },
            "variables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "default": {},
                        "required": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
                "description": "Declared template variables (save).",
            },
            "values": {
                "type": "object",
                "additionalProperties": {},
                "description": "Variable values for generate/preview.",
            },
            "output_path": {"type": "string", "description": "generate: destination file path."},
            "defaults": {
                "type": "object",
                "additionalProperties": {},
                "description": "save: extra metadata (documents: toc/cover; decks: aspect/background).",
            },
            "overwrite": {
                "type": "boolean",
                "description": "save: replace an existing template. Default true.",
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
        if action == "list":
            return {"success": True, "templates": self._store.list("templates")}
        name = params.get("name")
        if not name:
            return {"success": False, "error": f"`name` is required for action={action}"}
        try:
            if action == "save":
                return await self._save(name, params)
            if action == "delete":
                removed = self._store.delete("templates", str(name))
                return {"success": True, "deleted": removed, "name": name}
            template = self._store.load("templates", str(name))
            if template is None:
                return {"success": False, "error": f"template not found: {name!r}"}
            if action == "get":
                return {"success": True, "template": template}
            payload = _instantiate_template(template, params.get("values") or {})
            if action == "preview":
                return {"success": True, "name": template.get("name", name), "rendered": payload}
            return await self._generate(template, payload, params, context)
        except ValueError as e:
            return {"success": False, "error": str(e)}

    async def _save(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("kind") or "document"
        if kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}")
        if kind == "document" and not (params.get("content") or "").strip():
            raise ValueError("`content` is required for document templates")
        if kind == "deck" and not (params.get("slides") or []):
            raise ValueError("`slides` is required for deck templates")
        saved = self._store.save(
            "templates",
            str(name),
            {
                "name": name,
                "kind": kind,
                "description": params.get("description"),
                "variables": params.get("variables") or [],
                "content": params.get("content"),
                "slides": params.get("slides"),
                "defaults": params.get("defaults") or {},
            },
            overwrite=bool(params.get("overwrite", True)),
        )
        return {
            "success": True,
            "saved": saved,
            "name": name,
            "kind": kind,
            "usage": f"instantiate with action='generate', name='{name}', values={{...}}, output_path='...'",
        }

    async def _generate(
        self,
        template: dict[str, Any],
        payload: dict[str, Any],
        params: dict[str, Any],
        context: ToolPermissionContext | None,
    ) -> dict[str, Any]:
        output_path = params.get("output_path")
        if not output_path:
            raise ValueError("`output_path` is required for action=generate")
        kind = template.get("kind", "document")
        if kind == "deck":
            from bandi.tools.gen.slides_tool import SlidesGenerateTool

            return await SlidesGenerateTool().execute(
                {"slides": payload.get("slides") or [], "output_path": output_path}, context
            )
        from bandi.tools.gen.document_tool import DocumentGenerateTool

        return await DocumentGenerateTool().execute(
            {"content": payload.get("content") or "", "output_path": output_path}, context
        )


def _instantiate_template(template: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    env = Environment(undefined=Undefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
    kind = template.get("kind", "document")
    if kind == "deck":
        slides: list[dict[str, Any]] = []
        for slide in template.get("slides") or []:
            rendered_slide: dict[str, Any] = {}
            for field in ("title", "body", "takeaway"):
                if slide.get(field) is not None:
                    rendered_slide[field] = env.from_string(str(slide[field])).render(**values)
            for key, value in slide.items():
                if key not in rendered_slide:
                    rendered_slide[key] = value
            slides.append(rendered_slide)
        return {"slides": slides}
    content = template.get("content") or ""
    return {"content": env.from_string(content).render(**values)}
