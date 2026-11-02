# 功能：polite_http 共享库 — per-host 限速/抖动/429+5xx 重试（复用 web_fetch SSRF 防护 client）
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

import httpx

from bandi.tools.web_fetch import _assert_public_url, _build_guarded_client

logger = logging.getLogger(__name__)

_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_last_start_monotonic: dict[str, float] = {}


def _host_key(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
        return h or "_"
    except Exception:  # noqa: BLE001
        return "_"


def _merge_headers(kwargs: dict[str, Any], user_agent: str) -> dict[str, Any]:
    # 功能：为请求补充默认 UA（仅在调用方未显式给出时）
    headers = dict(kwargs.get("headers") or {})
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = user_agent
    return headers


@asynccontextmanager
async def host_request_gate(
    url: str, min_interval_ms: float = 300.0, jitter_ms_max: float = 150.0
) -> AsyncIterator[None]:
    # 功能：per-host 请求闸门——同一 host 串行 + 最小间隔 + 随机抖动
    host = _host_key(url)
    lock = _locks[host]
    min_s = max(0.0, min_interval_ms / 1000.0)
    jitter_s = max(0.0, jitter_ms_max / 1000.0)
    async with lock:
        now = time.monotonic()
        last = _last_start_monotonic.get(host, 0.0)
        if last > 0 and now - last < min_s:
            sleep_s = min_s - (now - last)
            if jitter_s > 0:
                sleep_s += random.uniform(0, jitter_s)
            await asyncio.sleep(max(0.0, sleep_s))
        _last_start_monotonic[host] = time.monotonic()
        yield


def _parse_retry_after(resp: httpx.Response) -> float | None:
    # 功能：解析 Retry-After 头（秒数或 HTTP 日期），非法返回 None
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return min(float(raw), 120.0)
    try:
        import email.utils

        exp = email.utils.parsedate_to_datetime(raw)
        if exp is None:
            return None
        return min(max(0.0, exp.timestamp() - time.time()), 120.0)
    except Exception:  # noqa: BLE001
        return None


async def polite_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    user_agent: str = "BandiBot/1.0",
    max_retries: int = 2,
    retry_backoff_base_ms: float = 500.0,
    min_interval_ms: float = 300.0,
    jitter_ms_max: float = 150.0,
    **kwargs: Any,
) -> httpx.Response:
    """功能：非流式 HTTP 请求——per-host 限速 + 429/5xx 指数退避重试（不解析跟随重定向外目标）"""
    kwargs = dict(kwargs)
    kwargs["headers"] = _merge_headers(kwargs, user_agent)
    base_ms = max(50.0, retry_backoff_base_ms)
    async with host_request_gate(url, min_interval_ms, jitter_ms_max):
        attempt = 0
        while True:
            try:
                resp = await client.request(method, url, **kwargs)
            except httpx.TimeoutException:
                if attempt >= max_retries:
                    raise
                await asyncio.sleep((base_ms / 1000.0) * (2**attempt))
                attempt += 1
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt >= max_retries:
                    resp.raise_for_status()
                wait = _parse_retry_after(resp)
                if wait is None:
                    wait = min((base_ms / 1000.0) * (2**attempt), 60.0)
                logger.warning(
                    "polite_http_retry url=%s status=%s attempt=%s sleep_s=%.1f",
                    url[:120],
                    resp.status_code,
                    attempt,
                    wait,
                )
                await asyncio.sleep(wait)
                attempt += 1
                continue
            resp.raise_for_status()
            return resp


async def polite_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    # 功能：带 SSRF 防护 + 限速重试的 GET
    await _assert_public_url(url)
    return await polite_request(client, "GET", url, **kwargs)


def build_polite_client(
    *, timeout: int = 20, allow_private: bool = False, transport: Any | None = None
) -> httpx.AsyncClient:
    # 功能：构建带 SSRF 防护（私网拦截 + IP pinning）的 AsyncClient 供 polite_http 使用；
    # transport 可注入（测试用 MockTransport）
    return _build_guarded_client(timeout, allow_private, transport=transport)
