# 功能：WebImageSearchTool — 图片搜索（Google CSE 门控；未配置 fail-open 返回空 + 引导）
from __future__ import annotations

import os
from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.web._polite_http import build_polite_client, polite_get

_MAX_RESULTS = 10


def _cse_credentials() -> tuple[str, str] | None:
    # 功能：读取 Google CSE key（env 门控）——未配置返回 None（调用方 fail-open）
    cse_key = os.environ.get("BANDI_CSE_API_KEY", "").strip()
    cse_id = os.environ.get("BANDI_CSE_CX", "").strip()
    if cse_key and cse_id:
        return cse_key, cse_id
    return None


class WebImageSearchTool(BaseTool):
    external_content: bool = True
    name: str = "web_image_search"
    description: str = (
        "Search the web for image URLs. When Google CSE credentials are configured "
        "(BANDI_CSE_API_KEY / BANDI_CSE_CX), returns matching HTTPS image URLs. "
        "If not configured, returns an empty list with guidance; pair with "
        "web_image_download to persist a chosen image locally."
    )
    category: ToolCategory = ToolCategory.WEB
    feature_group: str = "web"
    capability_tags: list[str] = [ToolCapability.NETWORK.value]
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "description": "Image search query."},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_RESULTS,
                "default": _MAX_RESULTS,
                "description": "Max results to return (default 10).",
            },
        },
    }

    def __init__(self, *, timeout: int = 20, transport: Any | None = None) -> None:
        self._timeout = timeout
        self._transport = transport

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        query = str(params.get("query") or "")
        if not query:
            return {"success": False, "error": "query is required"}
        creds = _cse_credentials()
        if creds is None:
            return {
                "success": True,
                "configured": False,
                "results": [],
                "guide": (
                    "Google CSE not configured. Set BANDI_CSE_API_KEY and BANDI_CSE_CX "
                    "to enable image search; continue without images or use web_scraper "
                    "to fetch a known URL."
                ),
            }
        cse_key, cse_id = creds
        max_result = min(max(int(params.get("max_results", _MAX_RESULTS)), 1), _MAX_RESULTS)
        params_url = {
            "q": query,
            "cx": cse_id,
            "key": cse_key,
            "searchType": "image",
            "num": str(max_result),
            "safe": "high",
        }
        try:
            async with build_polite_client(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await polite_get(
                    client, "https://www.googleapis.com/customsearch/v1", params=params_url
                )
                payload = resp.json()
            items = payload.get("items") or []
            results = [
                {
                    "title": it.get("title", ""),
                    "link": it.get("link", ""),
                    "mime": (it.get("mime") or "").strip(),
                }
                for it in items
                if it.get("link", "").startswith("https://")
            ]
            return {"success": True, "configured": True, "results": results}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "configured": True, "error": str(exc)}
