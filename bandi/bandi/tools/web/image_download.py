# 功能：WebImageDownloadTool — 下载远程 HTTPS 图片到工作区并返回本地路径
from __future__ import annotations

import re
from typing import Any

from bandi.tools.base import ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool
from bandi.tools.web._polite_http import build_polite_client, polite_get
from bandi.tools.web._robots import assert_fetch_allowed

_SOFT_MAX_BYTES = 25 * 1024 * 1024
_IMG_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
}
_SLUG_RE = re.compile(r"[^\w\-]+")


def _slug(name: str) -> str:
    # 功能：URL 文件名/标题 → 安全 slug（保留中文与字母数字）
    return _SLUG_RE.sub("-", name).strip("-").lower()[:80] or "image"


class WebImageDownloadTool(FsTool):
    external_content: bool = True
    name: str = "web_image_download"
    description: str = (
        "Download an image from a public HTTP(S) URL into the session workspace. "
        "Returns the local file path and detected mime type. The file is stored "
        "under the workspace and can be referenced by other tools."
    )
    category: ToolCategory = ToolCategory.WEB
    feature_group: str = "web"
    capability_tags: list[str] = [ToolCapability.NETWORK.value, ToolCapability.FILE_WRITE.value]
    risk_level: str = "low"
    is_read_only: bool = False
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["url"],
        "additionalProperties": False,
        "properties": {
            "url": {"type": "string", "description": "Public HTTP(S) image URL."},
            "output_path": {
                "type": "string",
                "description": "Destination path (default: <workspace>/images/<slug>.<ext>).",
            },
        },
    }

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        timeout: int = 30,
        transport: Any | None = None,
    ) -> None:
        self._allow_private = allow_private_networks
        self._timeout = timeout
        self._transport = transport

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        url = str(params.get("url") or "")
        if not url:
            return {"success": False, "error": "url is required"}
        try:
            async with build_polite_client(
                timeout=self._timeout,
                allow_private=self._allow_private,
                transport=self._transport,
            ) as client:
                await assert_fetch_allowed(client, url)
                resp = await polite_get(client, url, timeout=self._timeout)
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type not in _IMG_EXT_BY_MIME:
                    return {
                        "success": False,
                        "error": f"unsupported image content type: {content_type or 'unknown'}",
                    }
                data = resp.content
                if len(data) > _SOFT_MAX_BYTES:
                    return {
                        "success": False,
                        "error": f"image exceeds {_SOFT_MAX_BYTES} bytes",
                    }
                raw_name = _slug(url.rsplit("/", 1)[-1])
                ext = _IMG_EXT_BY_MIME[content_type]
                if "." in url.rsplit("/", 1)[-1]:
                    raw_name = _slug(url.rsplit("/", 1)[-1].rsplit(".", 1)[0])
                custom = params.get("output_path")
                if isinstance(custom, str) and custom.strip():
                    rel = custom.strip()
                else:
                    rel = f"images/{raw_name}{ext}"
                path = self._resolve_output(rel, context)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return {
                    "success": True,
                    "file": str(path),
                    "mime": content_type,
                    "size_bytes": len(data),
                }
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
