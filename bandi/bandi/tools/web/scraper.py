# 功能：WebScraperTool — CSS 选择器抓取网页内容（stdlib html.parser，无三方解析依赖）
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from bandi.tools.base import BaseTool, ToolCapability, ToolCategory, ToolPermissionContext
from bandi.tools.web._polite_http import build_polite_client, polite_get
from bandi.tools.web._robots import assert_fetch_allowed

_SELECTOR_TOKEN_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9]*)|(\.([a-zA-Z0-9_-]+))|(#([a-zA-Z0-9_-]+))|\[([a-zA-Z0-9_-]+)(?:=(\"[^\"]*\"|'[^']*'|[^\]\"']+))?\]"
)
_MAX_RESULT_CHARS = 200_000
_MAX_MATCHES = 50


@dataclass(frozen=True)
class CssSelector:
    # 功能：分解后的 CSS 选择器（tag/classes/ids/attr），供 HTMLParser 流式匹配
    tag: str | None
    classes: tuple[str, ...]
    ids: tuple[str, ...]
    attrs: tuple[tuple[str, str], ...]


def parse_css_selector(raw: str) -> CssSelector | None:
    # 功能：解析单标签 CSS 选择器（不支持后代组合器的复杂表达式，匹配精确路径/简化形式）
    text = (raw or "").strip()
    if not text or re.search(r"[ >+~]", text):
        return None
    tag: str | None = None
    classes: list[str] = []
    ids: list[str] = []
    attrs: list[tuple[str, str]] = []
    pos = 0
    for m in _SELECTOR_TOKEN_RE.finditer(text):
        if m.start() != pos:
            return None
        pos = m.end()
        if m.group(1):
            if tag is not None:
                return None
            tag = m.group(1)
        elif m.group(3):
            classes.append(m.group(3))
        elif m.group(5):
            ids.append(m.group(5))
        elif m.group(6):
            name = m.group(6)
            value = (m.group(7) or "").strip("\"'")
            attrs.append((name, value))
    if pos != len(text):
        return None
    return CssSelector(tag=tag, classes=tuple(classes), ids=tuple(ids), attrs=tuple(attrs))


class _MatchTracker(HTMLParser):
    # 功能：AST 流式匹配器——命中选择器时记录元素文本/属性，支持深入单一元素文本提取
    def __init__(self, selector: CssSelector, max_matches: int = _MAX_MATCHES) -> None:
        super().__init__()
        self._sel = selector
        self._max_matches = max_matches
        self._stack: list[dict[str, Any]] = []
        self._results: list[dict[str, Any]] = []
        self._capture: list[str] | None = None

    def _matches(self, tag: str, attrs: dict[str, str]) -> bool:
        sel = self._sel
        if sel.tag is not None and tag != sel.tag:
            return False
        if sel.classes and any(not c for c in sel.classes):
            return False
        if sel.classes:
            class_attr = (attrs.get("class") or "").split()
            if not all(c in class_attr for c in sel.classes):
                return False
        for i in sel.ids:
            if attrs.get("id") != i:
                return False
        for name, value in sel.attrs:
            got = attrs.get(name)
            if value:
                if got != value:
                    return False
            elif got is None:
                return False
        return True

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        node = {"tag": tag, "attrs": attrs, "text": []}
        if self._matches(tag, attrs) and len(self._results) < self._max_matches:
            node["matched"] = True
            self._capture = node["text"]
        self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        node = self._stack.pop()
        if node["tag"] != tag:
            return
        if node.get("matched") and len(self._results) < self._max_matches:
            text = self._collapse(node["text"])
            self._results.append({"tag": tag, "text": text, "attrs": node["attrs"]})
            if self._capture is node["text"]:
                self._capture = self._stack[-1]["text"] if self._stack else None

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if self._matches(tag, attrs) and len(self._results) < self._max_matches:
            self._results.append({"tag": tag, "text": "", "attrs": attrs})

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture.append(data)

    @staticmethod
    def _collapse(chunks: list[str]) -> str:
        text = "".join(chunks)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines)[:_MAX_RESULT_CHARS]

    def matches(self) -> list[dict[str, Any]]:
        # 功能：返回命中结果（element tag/text/attrs），供execute消费
        return list(self._results)


class WebScraperTool(BaseTool):
    external_content: bool = True
    name: str = "web_scraper"
    description: str = (
        "Fetch a web page and extract content using CSS selectors. Pass `selectors` "
        "as a dict of field name -> CSS selector (tag, .class, #id, or tag[attr=value]). "
        "Returns title, status and a data map of extracted text per field."
    )
    category: ToolCategory = ToolCategory.WEB
    risk_level: str = "low"
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    capability_tags: list[str] = [ToolCapability.NETWORK.value]
    feature_group: str = "web"
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["url"],
        "additionalProperties": False,
        "properties": {
            "url": {"type": "string", "description": "Public HTTP(S) page URL to scrape."},
            "selectors": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Named CSS selectors: {field: selector}. Required unless `selector` given.",
            },
            "selector": {
                "type": "string",
                "description": "Single CSS selector (alternative to `selectors`).",
            },
            "multiple": {
                "type": "boolean",
                "default": False,
                "description": "Return all matches for each selector (True) or first match (False).",
            },
        },
    }

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        timeout: int = 25,
        transport: Any | None = None,
    ) -> None:
        self._allow_private = allow_private_networks
        self._timeout = timeout
        self._transport = transport

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        url = str(params.get("url") or "")
        selectors: dict[str, str] = {}
        raw_sels = params.get("selectors")
        if isinstance(raw_sels, dict):
            selectors = {str(k): str(v) for k, v in raw_sels.items()}
        single = params.get("selector")
        if isinstance(single, str) and single.strip():
            selectors["result"] = single.strip()
        multiple = bool(params.get("multiple", False))
        if not selectors:
            return {"success": False, "error": "selectors or selector is required"}
        async with build_polite_client(
            timeout=self._timeout,
            allow_private=self._allow_private,
            transport=self._transport,
        ) as client:
            await assert_fetch_allowed(client, url)
            try:
                resp = await polite_get(client, url, timeout=self._timeout)
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": str(exc)}
            content_type = resp.headers.get("content-type", "")
            if "html" not in content_type and not content_type.startswith("text/"):
                return {
                    "success": False,
                    "error": f"unsupported content type: {content_type}",
                }
            html = resp.text
            if len(html.encode("utf-8")) > 2 * 1024 * 1024:
                html = html.encode("utf-8")[: 2 * 1024 * 1024].decode("utf-8", "ignore")
            data: dict[str, Any] = {}
            for field, sel_text in selectors.items():
                parsed = parse_css_selector(sel_text)
                if parsed is None:
                    data[field] = {"error": f"unsupported selector: {sel_text!r}"}
                    continue
                tracker = _MatchTracker(parsed)
                try:
                    tracker.feed(html)
                except Exception:  # noqa: BLE001
                    data[field] = {"error": "parse failure"}
                    continue
                found = tracker.matches()
                if not found:
                    data[field] = None
                elif multiple:
                    data[field] = [m["text"] for m in found]
                else:
                    data[field] = found[0]["text"]
        return {
            "success": True,
            "url": url,
            "status": resp.status_code,
            "data": data,
        }
