# 功能：CanvasPublishTool — 把 HTML/组件树发布为工作区可打开预览的产物文件
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

_TITLE_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
_MAX_HTML_FILES = 40
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

_EMBED_FILE_KEYS = ("youtube.com", "youtube-nocookie.com", "vimeo.com", "openstreetmap.org")
_EMBED_EXACT_HOSTS = (
    "www.youtube.com",
    "www.youtube-nocookie.com",
    "player.vimeo.com",
    "vimeo.com",
    "openstreetmap.org",
    "www.openstreetmap.org",
)
_EMBED_GOOGLE_MAPS_HOSTS = ("maps.google.com",)


def _slugify(title: str) -> str:
    # 功能：标题转目录名（保留中文/字母/数字/连字符，折叠空格与特殊字符）
    s = _TITLE_SLUG_RE.sub("-", title.strip()).strip("-").lower()
    return s[:80] or "canvas"


def _is_allowed_embed_host(url: str) -> bool:
    # 功能：hostname 级 embed 白名单校验——裸主域或受信子域，杜绝子串伪造域
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _EMBED_EXACT_HOSTS or host in _EMBED_GOOGLE_MAPS_HOSTS:
        return True
    if host == "google.com":
        return parsed.path.startswith("/maps")
    return any(host == key or host.endswith(f".{key}") for key in _EMBED_FILE_KEYS)


def _assert_relative(rel: str) -> str:
    # 功能：校验产物相对路径（杜绝绝对路径 / ../ ./ 穿越，仅允许单层子目录）
    key = (rel or "").strip().replace("\\", "/")
    if not key:
        raise ValueError("file key is required")
    if key.startswith(("/", "..", "http://", "https://", "data:", "blob:")):
        raise ValueError(f"unsafe file key: {rel!r}")
    segments = key.split("/")
    if any(seg in ("..", ".") for seg in segments):
        raise ValueError(f"unsafe file key: {rel!r}")
    if len(segments) > 2:
        raise ValueError(f"unsafe file key: {rel!r}")
    return "/".join(segments)


class CanvasPublishTool(FsTool):
    name: str = "canvas_publish"
    description: str = (
        "Publish a generated HTML page (single file or multi-file map) into the "
        "workspace canvas directory and return a local preview path the user can "
        "open. Use when the user explicitly asked for a webpage / printable report / "
        "HTML artifact. For smaller component-style UIs prefer `emit_ui_tree` instead. "
        "mode=html: pass `html` (one document) or `html_files` map with `html_bundle_entry`. "
        "mode=embed_url: pass an allow-listed embed URL (YouTube/Vimeo/Google Maps/OSM). "
        "mode=gen_ui: persist a validated emitted ui tree."
    )
    category: ToolCategory = ToolCategory.CANVAS
    feature_group: str = "canvas"
    capability_tags: list[str] = [ToolCapability.FILE_WRITE.value]
    risk_level: str = "low"
    is_read_only: bool = False
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["title", "mode"],
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "mode": {"type": "string", "enum": ["html", "embed_url", "gen_ui"]},
            "html": {
                "type": "string",
                "description": "Full HTML document body for mode=html (single file).",
            },
            "html_files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Relative path → file body for mode=html multi-file pages.",
            },
            "html_bundle_entry": {
                "type": "string",
                "description": "Entry file (default index.html) when html_files used.",
            },
            "embed_url": {
                "type": "string",
                "description": "Allow-listed embed URL for mode=embed_url.",
            },
            "ui_tree": {
                "type": "object",
                "description": "Validated ui tree to persist for mode=gen_ui.",
            },
        },
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        title = str(params.get("title") or "").strip()
        mode = str(params.get("mode") or "")
        if not title:
            return {"success": False, "error": "title is required"}
        if mode not in ("html", "embed_url", "gen_ui"):
            return {"success": False, "error": f"unsupported mode: {mode}"}
        try:
            rel_dir = f"canvas/{_slugify(title)}"
            if mode == "html":
                files: dict[str, str] = {}
                html = params.get("html")
                if isinstance(html, str) and html.strip():
                    files["index.html"] = html
                raw_files = params.get("html_files")
                if isinstance(raw_files, dict) and raw_files:
                    files.update({_assert_relative(k): str(v) for k, v in raw_files.items()})
                entry = params.get("html_bundle_entry")
                if not files:
                    return {
                        "success": False,
                        "error": "mode=html requires `html` or `html_files`",
                    }
                html_keys = [k for k in files if k.endswith(".html")]
                if not html_keys:
                    raise ValueError("html_files map must include at least one .html entry file")
                if "index.html" not in files:
                    if isinstance(entry, str) and entry.strip() and entry in files:
                        pass
                    else:
                        sole = html_keys[0] if len(html_keys) == 1 else "index.html"
                        if sole not in files:
                            raise ValueError(
                                "html_files map must include index.html or "
                                "html_bundle_entry naming an entry"
                            )
                total = sum(
                    len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in files.items()
                )
                if total > _MAX_PAYLOAD_BYTES:
                    return {
                        "success": False,
                        "error": f"html payload exceeds {_MAX_PAYLOAD_BYTES} bytes",
                    }
                if len(files) > _MAX_HTML_FILES:
                    return {
                        "success": False,
                        "error": f"html_files exceeds {_MAX_HTML_FILES} files",
                    }
                entry_key = (
                    entry.strip()
                    if isinstance(entry, str) and entry.strip() in files
                    else ("index.html" if "index.html" in files else html_keys[0])
                )
                written: list[str] = []
                for key, body in files.items():
                    path: Path = self._resolve_output(f"{rel_dir}/{key}", context)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(body, encoding="utf-8")
                    written.append(str(path))
                return {
                    "success": True,
                    "preview_path": f"{rel_dir}/{entry_key}",
                    "files": written,
                    "mode": mode,
                }
            if mode == "embed_url":
                url = str(params.get("embed_url") or "")
                if not url:
                    return {"success": False, "error": "embed_url is required for mode=embed_url"}
                if not url.startswith("https://") or not _is_allowed_embed_host(url):
                    return {
                        "success": False,
                        "error": "embed_url must be an https URL from an allow-listed embed host",
                    }
                embed_title = url.replace('"', "")
                embed = (
                    f'<iframe src="{embed_title}" loading="lazy" allowfullscreen '
                    'style="width:100%;height:100%;border:0"></iframe>'
                )
                path: Path = self._resolve_output(f"{rel_dir}/index.html", context)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(embed, encoding="utf-8")
                return {
                    "success": True,
                    "preview_path": f"{rel_dir}/index.html",
                    "files": [str(path)],
                    "mode": mode,
                }
            ui_tree = params.get("ui_tree")
            if not isinstance(ui_tree, dict):
                return {"success": False, "error": "ui_tree is required for mode=gen_ui"}
            import json

            payload = json.dumps({"root": ui_tree}, ensure_ascii=False, indent=2)
            path: Path = self._resolve_output(f"{rel_dir}/ui_tree.json", context)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
            return {
                "success": True,
                "preview_path": f"{rel_dir}/ui_tree.json",
                "files": [str(path)],
                "mode": mode,
            }
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
