# 功能：网页抓取工具（WebFetchTool HTML 转文本 + 链接提取 + SSRF 私网拦截 + 重定向/DNS rebinding 防护）
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from httpcore import AsyncConnectionPool
from httpcore._backends.auto import AutoBackend

from bandi.tools.base import (
    BaseTool,
    ToolCategory,
    ToolPermissionContext,
    ToolProgressEvent,
)

_PRIVATE_IPV4_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
)
_PRIVATE_IPV6_NETWORKS = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::ffff:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    networks = (
        _PRIVATE_IPV6_NETWORKS if isinstance(ip, ipaddress.IPv6Address) else _PRIVATE_IPV4_NETWORKS
    )
    return any(ip in network for network in networks)


def _resolve_public_ips(host: str) -> list[str]:
    # 功能：解析 host 全部 IP 并校验均为公网（任一私网抛 ValueError），返回去重 IP 列表
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise ValueError(f"unable to resolve host {host!r}") from exc
    seen: list[str] = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_private_ip(ip):
            raise ValueError(f"blocked private network address: {host}")
        text = str(ip)
        if text not in seen:
            seen.append(text)
    if not seen:
        raise ValueError(f"unable to resolve host {host!r}")
    return seen


async def _resolve_public_ips_async(host: str) -> list[str]:
    # 功能：DNS 解析移线程池——SSRF 校验不得同步阻塞事件循环（解析慢时 agent 停摆）
    return await asyncio.to_thread(_resolve_public_ips, host)


async def _assert_public_url(url: str) -> None:
    # 功能：SSRF 防护——私网/回环/链路本地地址拦截（直连 IP 与域名解析均校验）
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"invalid URL: {url!r}")
    await _resolve_public_ips_async(host)


async def _assert_request_public(request: httpx.Request) -> None:
    # 功能：SSRF 重定向防护——每次实际发送请求前校验目标 URL（含重定向跳转目标）
    await _assert_public_url(str(request.url))


class _PinningBackend:
    """连接层 SSRF 防护——DNS rebinding 根治。

    校验与连接之间的二次 DNS 解析是 rebinding 窗口：攻击者可让校验时解析公网 IP
    连接时解析私网 IP（TTL=0 / 双 A 记录轮换）。本 backend 在 connect_tcp 时
    重新解析 host、校验全部 IP 为公网、再用解析出的公网 IP 直连——解析结果
    直接用于连接，杜绝二次解析窗口。
    """

    def __init__(self) -> None:
        self._inner = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> Any:
        ips = await _resolve_public_ips_async(host)
        return await self._inner.connect_tcp(ips[0], port, timeout, local_address, socket_options)

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any | None = None
    ) -> Any:
        return await self._inner.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        return await self._inner.sleep(seconds)


class _SSRFCheckTransport(httpx.AsyncBaseTransport):
    """SSRF 防护 transport——连接层 IP pinning（防 DNS rebinding）+ 事件层校验。"""

    def __init__(
        self,
        allow_private: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_private = allow_private
        if transport is not None:
            self._inner = transport
        else:
            self._inner = httpx.AsyncHTTPTransport()
            if not allow_private:
                # 通过私有属性注入 PinningBackend（防 DNS rebinding）
                try:
                    self._inner._pool = AsyncConnectionPool(network_backend=_PinningBackend())
                except AttributeError:
                    pass  # httpx 新版无 _pool 属性，降级为无 pinning

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)


def _build_guarded_client(
    timeout: int,
    allow_private: bool,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    # 功能：构建带 SSRF 防护的 AsyncClient——入口校验 + 每跳 hook 校验 + 连接层 IP pinning 校验
    guarded: httpx.AsyncBaseTransport = (
        _SSRFCheckTransport(allow_private, transport=transport)
        if not allow_private
        else (transport or httpx.AsyncHTTPTransport())
    )
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        transport=guarded,
        event_hooks={"request": [_assert_request_public]} if not allow_private else None,
    )


class _HTMLToTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._result: list[str] = []
        self._skip: int = 0
        self._list_level: int = 0
        self._in_pre: bool = False
        self._in_link: bool = False
        self._link_href: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "noscript", "iframe", "object", "embed"):
            self._skip += 1
        if self._skip:
            return
        if tag in ("h1", "h2", "h3", "h4") or tag == "br" or tag == "p":
            self._result.append("\n")
        if tag == "li":
            self._result.append("\n  - ")
        elif tag == "tr":
            self._result.append("\n")
        elif tag == "td":
            self._result.append("  ")
        elif tag == "a":
            self._in_link = True
            for attr_name, attr_val in attrs:
                if attr_name == "href" and attr_val:
                    self._link_href = attr_val
        elif tag == "pre":
            self._in_pre = True
            self._result.append("\n")
        elif tag == "img":
            for attr_name, attr_val in attrs:
                if attr_name in ("alt", "src") and attr_val:
                    self._result.append(f"[{attr_val}] ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "noscript", "iframe", "object", "embed"):
            self._skip -= 1
        if self._skip:
            return
        if tag in ("p", "h1", "h2", "h3", "h4", "div", "pre"):
            self._result.append("\n")
        elif tag == "a" and self._in_link:
            if self._link_href:
                self._result.append(f" ({self._link_href})")
            self._in_link = False
            self._link_href = ""

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip() if not self._in_pre else data
        if text:
            self._result.append(text)

    def get_text(self) -> str:
        lines = "".join(self._result).splitlines()
        collapsed = []
        blank = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                blank += 1
            else:
                blank = 0
            if blank <= 1:
                collapsed.append(stripped)
        return "\n".join(collapsed)


class WebFetchTool(BaseTool):
    external_content: bool = True
    name: str = "web_fetch"
    description: str = "Fetch content from a URL and return as readable text"
    category: ToolCategory = ToolCategory.WEB
    risk_level: str = "low"
    is_read_only: bool = True
    parameters: dict[str, Any] = {
        "type": "object",
        "required": ["url"],
        "additionalProperties": False,
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to fetch",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30, max 60)",
                "default": 30,
            },
        },
    }
    is_concurrency_safe: bool = True

    _UA: str = "Mozilla/5.0 (compatible; BandiBot/1.0)"

    def __init__(
        self,
        timeout_default: int = 30,
        timeout_max: int = 60,
        max_bytes: int = 5242880,
        fallback_preview_chars: int = 2000,
        allow_private_networks: bool = False,
    ):
        self._timeout_default = timeout_default
        self._timeout_max = timeout_max
        self._max_bytes = max_bytes
        self._fallback_preview_chars = fallback_preview_chars
        self._allow_private_networks = allow_private_networks

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> str:
        url = params["url"]
        timeout = max(1, min(int(params.get("timeout", self._timeout_default)), self._timeout_max))

        if not self._allow_private_networks:
            await _assert_public_url(url)

        async with _build_guarded_client(timeout, self._allow_private_networks) as client:
            async with client.stream("GET", url, headers={"User-Agent": self._UA}) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not content_type.startswith("text/") and "html" not in content_type:
                    return f"unsupported content type: {content_type}"
                # max_bytes 只截后处理结果（内存峰值不受控）
                collected = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    collected.extend(chunk)
                    if len(collected) > self._max_bytes:
                        truncated = True
                        break
                text = bytes(collected[: self._max_bytes]).decode("utf-8", errors="ignore")
                if truncated:
                    text += "\n... [truncated]"
            parser = _HTMLToTextParser()
            parser.feed(text)
            result = parser.get_text().strip()
            if not result:
                result = text[: self._fallback_preview_chars]
            source = f"\n\n---\nSource: {resp.url}"
            return result + source

    async def stream(self, params: dict[str, Any], context: ToolPermissionContext | None = None):
        # 功能：流式覆盖——拉取前上报 fetch_started 状态，完成后上报 complete 结果。
        # 提供给前端工具时间线的增量进度（生产落地示例）。
        yield ToolProgressEvent(
            type="status",
            data={"step": "fetch_started", "url": params.get("url", "")},
            timestamp=time.time(),
        )
        result = await self.execute(params, context=context)
        yield ToolProgressEvent(type="complete", data={"result": result}, timestamp=time.time())
