# 功能：TemplateFillerTool — Jinja2 模板渲染（变量替换/条件/循环/内置安全滤波器）
from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from jinja2 import Environment, StrictUndefined, Undefined, select_autoescape
from jinja2.exceptions import TemplateError, TemplateSyntaxError, UndefinedError

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool


class TemplateFillerTool(FsTool):
    name: str = "template_filler"
    description: str = (
        "Render a Jinja2 template with variables: dot-notation substitution, "
        "if/for blocks, and built-in filters (date, currency, number, slugify, "
        "to_json, from_json, pluralize, default). Template source can be a "
        "`template_string`, a `template_path`, or a `template_url`. Variables come "
        "from `variables` and/or a JSON/YAML `data_file`. Optionally write the "
        "result to `output_path`."
    )
    category: ToolCategory = ToolCategory.GEN
    feature_group: str = "gen"
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    risk_level: str = "low"
    is_concurrency_safe: bool = True
    is_idempotent: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "template_source": {
                "type": "string",
                "enum": ["string", "file", "url"],
                "description": "Where the template comes from. Default string.",
            },
            "template_string": {"type": "string", "description": "Template body (string source)."},
            "template_path": {"type": "string", "description": "Template file path (file source)."},
            "template_url": {"type": "string", "description": "Template URL (url source)."},
            "variables": {
                "type": "object",
                "additionalProperties": {},
                "description": "Variables to substitute.",
            },
            "data_file": {
                "type": "string",
                "description": "Path to a JSON/YAML file with variables.",
            },
            "strict_mode": {
                "type": "boolean",
                "description": "Raise on undefined variables. Default false.",
            },
            "output_format": {
                "type": "string",
                "enum": ["text", "html", "json", "yaml", "markdown"],
                "description": "Output classification. Default text.",
            },
            "output_path": {"type": "string", "description": "Optional output file path."},
        },
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            template = await self._load_template(params, context)
            variables = self._load_variables(params, context)
            env = self._build_env(params)
            try:
                rendered = env.from_string(template).render(**variables)
            except TemplateSyntaxError as e:
                return {
                    "success": False,
                    "error": f"template syntax error at line {e.lineno}: {e.message}",
                }
            except UndefinedError as e:
                return {"success": False, "error": f"undefined variable: {e}"}
            except TemplateError as e:
                return {"success": False, "error": f"template rendering error: {e}"}

            output_path = params.get("output_path")
            result: dict[str, Any] = {
                "success": True,
                "rendered": rendered,
                "rendered_length": len(rendered),
                "variables_used": list(variables.keys()),
                "output_format": params.get("output_format", "text"),
            }
            if output_path:
                path = self._resolve_output(output_path, context)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(rendered, encoding="utf-8")
                result["output_path"] = str(path)
                result["size_bytes"] = path.stat().st_size
            return result
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    async def _load_template(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> str:
        source = params.get("template_source", "string")
        if source == "file":
            path = self._resolve_output(params.get("template_path", ""), context)
            if not path.is_file():
                raise ValueError(f"template file not found: {path}")
            return path.read_text(encoding="utf-8")
        if source == "url":
            url = params.get("template_url", "")
            if not url:
                raise ValueError("template_url is required for url source")
            scheme = urlsplit(url).scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError(f"template_url scheme must be http/https, got {scheme!r}")
            from bandi.tools.web_fetch import _assert_public_url

            await _assert_public_url(url)
            return await asyncio.to_thread(self._fetch_url_text, url)
        template_string = params.get("template_string", "")
        if not template_string.strip():
            raise ValueError("template_string is required for string source")
        return template_string

    @staticmethod
    def _fetch_url_text(url: str) -> str:
        # 功能：拉取 url 模板源（经线程池调用，不阻塞事件循环）
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8")

    def _load_variables(
        self, params: dict[str, Any], context: ToolPermissionContext | None
    ) -> dict[str, Any]:
        variables: dict[str, Any] = {}
        data_file = params.get("data_file")
        if data_file:
            path = self._resolve_output(data_file, context)
            if not path.is_file():
                raise ValueError(f"data file not found: {path}")
            content = path.read_text(encoding="utf-8")
            if path.suffix.lower() in (".yaml", ".yml"):
                import yaml

                loaded = yaml.safe_load(content)
            elif path.suffix.lower() == ".json":
                loaded = json.loads(content)
            else:
                raise ValueError(f"unsupported data file format: {path.suffix}")
            if isinstance(loaded, dict):
                variables.update(loaded)
        if params.get("variables"):
            variables.update(params["variables"])
        return variables

    def _build_env(self, params: dict[str, Any]) -> Environment:
        strict_mode = bool(params.get("strict_mode"))
        autoescape = select_autoescape(
            enabled_extensions=("html", "htm", "xml"),
            default_for_string=params.get("output_format") in ("html", "xml"),
        )
        env = Environment(
            loader=None,
            undefined=StrictUndefined if strict_mode else Undefined,
            autoescape=autoescape,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["format_date"] = _format_date
        env.filters["format_currency"] = _format_currency
        env.filters["format_number"] = _format_number
        env.filters["slugify"] = _slugify
        env.filters["to_json"] = _to_json
        env.filters["from_json"] = _from_json
        env.filters["pluralize"] = _pluralize
        env.filters["default_if_none"] = _default_if_none
        env.globals["now"] = _now
        return env


def _format_date(value: Any, fmt: str = "%Y-%m-%d") -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime(fmt)
    return str(value)


def _format_currency(value: Any, symbol: str = "", decimals: int = 2) -> str:
    try:
        return f"{symbol}{float(value):,.{decimals}f}"
    except (ValueError, TypeError):
        return str(value)


def _format_number(value: Any, decimals: int = 0, thousands_sep: str = ",") -> str:
    try:
        num = float(value)
        if decimals == 0:
            return f"{int(num):,}".replace(",", thousands_sep)
        return f"{num:,.{decimals}f}".replace(",", thousands_sep)
    except (ValueError, TypeError):
        return str(value)


def _slugify(value: str) -> str:
    s = str(value).lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"[-\s]+", "-", s)


def _to_json(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, indent=indent, ensure_ascii=False, default=str)


def _from_json(value: str) -> Any:
    return json.loads(value)


def _pluralize(value: int, singular: str = "", plural: str = "s") -> str:
    return singular if value == 1 else plural


def _default_if_none(value: Any, default: Any = "") -> Any:
    return default if value is None else value


def _now(fmt: str | None = None) -> str:
    now = datetime.now()
    return now.strftime(fmt) if fmt else now.isoformat()
