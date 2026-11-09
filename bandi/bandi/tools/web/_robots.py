# 功能：robots_policy 共享库 — robots.txt 检查（fail-open + per-host 内存缓存）
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from bandi.tools.web._polite_http import polite_get

_CACHE_TTL_SECS = 3600.0
_LOCK = threading.Lock()
_cache: dict[str, tuple[float, RobotFileParser]] = {}

_UA = "BandiBot/1.0"


def _allow_all() -> RobotFileParser:
    # 功能：robots.txt 缺失/不可读时的放开规则（fail-open，不阻塞本地优先工作流）
    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Allow: /"])
    return parser


def _host_of(url: str) -> tuple[str, str]:
    # 功能：提取 host 与 robots.txt 根 URL；非法 scheme/host 返回 (None, None)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "", ""
    host = parsed.hostname.lower()
    scheme = parsed.scheme
    port = parsed.port
    netloc = f"{host}:{port}" if port else host
    return host, f"{scheme}://{netloc}/robots.txt"


async def _fetch_robots(client: httpx.AsyncClient, robots_url: str) -> RobotFileParser:
    # 功能：拉取并解析 robots.txt；失败/fail-open 返回 _allow_all
    try:
        resp = await polite_get(client, robots_url, timeout=10.0)
        text = resp.text[: 64 * 1024]
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        return parser
    except Exception:  # noqa: BLE001
        return _allow_all()


async def assert_fetch_allowed(client: httpx.AsyncClient, url: str) -> None:
    # 功能：断言 URL 未被 robots.txt 禁止——命中或不可读抛 ValueError；未命中静默通过。
    # 缓存 miss 时先 fetch 后写缓存：async 单线程事件循环下不得在 threading.Lock
    # 临界区内 await（会阻塞循环线程形成永久死锁）；并发 miss 容忍重复只读 fetch。
    host, robots_url = _host_of(url)
    if not host:
        return
    now = time.time()
    cached = _cache.get(host)
    if cached is not None and now - cached[0] <= _CACHE_TTL_SECS:
        parser = cached[1]
    else:
        parser = await _fetch_robots(client, robots_url)
        _cache[host] = (now, parser)
    if not parser.can_fetch(_UA, url):
        raise ValueError(f"robots.txt disallows fetch: {url}")
    return None


def clear_robots_cache() -> None:
    # 功能：测试辅助——清空 per-host robots 缓存
    global _cache
    with _LOCK:
        _cache = {}
