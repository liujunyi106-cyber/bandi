from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.robotparser import RobotFileParser

import httpx
import pytest

from bandi.tools.web_fetch import _assert_public_url, _PinningBackend

# 功能：契约——SSRF 校验链 DNS 解析必须在线程池执行（不得同步阻塞事件循环）


pytestmark = pytest.mark.asyncio


def _install_dns_spy(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> list[int]:
    """替换模块内 socket.getaddrinfo 为不联网的确定性解析，记录调用线程 id。"""
    import bandi.tools.web_fetch as wf

    seen: list[int] = []

    def _fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list:
        seen.append(threading.current_thread().ident)
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(wf.socket, "getaddrinfo", _fake_getaddrinfo)
    return seen


async def test_public_url_check_dns_offloads_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（平台/边缘 · web_fetch 同步 DNS 修复）：_assert_public_url 的 DNS 解析
    必须离开事件循环线程——此前 socket.getaddrinfo 在循环内同步调用，解析慢/挂起
    时整个 agent 停摆。"""
    main_thread = threading.current_thread().ident
    seen = _install_dns_spy(monkeypatch)

    await _assert_public_url("https://example.com/")

    assert seen, "解析必须发生"
    assert all(tid != main_thread for tid in seen), "DNS 必须在线程池执行（不得阻塞事件循环）"


async def test_pinning_backend_dns_offloads_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约（web_fetch DNS · pinning 链）：connect_tcp 的二次解析同样不得阻塞
    事件循环，且 pinning 语义保持（以解析出的公网 IP 直连）。"""
    main_thread = threading.current_thread().ident
    seen = _install_dns_spy(monkeypatch)
    backend = _PinningBackend()
    connected: list[str] = []

    async def _fake_connect(
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> object:
        connected.append(host)
        return object()

    monkeypatch.setattr(backend._inner, "connect_tcp", _fake_connect)

    await backend.connect_tcp("example.com", 443)

    assert connected == ["93.184.216.34"], "必须以解析出的公网 IP 直连（pinning 语义保持）"
    assert seen and all(tid != main_thread for tid in seen), "DNS 必须在线程池执行"


# ==================== robots_concurrency ====================
# 功能：契约——robots 缓存并发访问不得死锁（临界区内 await 会卡死事件循环线程）


_REPO = Path(__file__).resolve().parents[2]  # bandi 包父目录（子进程 cwd）

_CONCURRENT_SCENARIO = textwrap.dedent(
    """
    import asyncio

    from bandi.tools.web import _robots


    async def slow_fetch(client, robots_url):
        await asyncio.sleep(0.1)
        from urllib.robotparser import RobotFileParser
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Allow: /"])
        return parser


    async def scenario():
        _robots._fetch_robots = slow_fetch
        _robots.clear_robots_cache()
        await asyncio.gather(
            _robots.assert_fetch_allowed(None, "https://host-a.example/page"),
            _robots.assert_fetch_allowed(None, "https://host-b.example/page"),
        )


    asyncio.run(scenario())
    print("DONE")
    """
)


@pytest.mark.timing
def test_contract_robots_concurrent_cache_miss_no_deadlock() -> None:
    """契约（并发·破坏者）：两个协程并发经缓存 miss 路径拉取 robots.txt 必须都
    能完成——临界区内 await 会阻塞事件循环线程形成永久死锁。场景在**子进程**中
    执行 + 父进程超时守门：红灯以 TimeoutExpired 呈现（子进程被 kill），不会把
    持锁死锁带入本进程污染整个测试套件。

    `timing`：父进程守门预算 ，而子进程含解释器冷启动 + 真实网络抓取
    （隔离单跑 ~）；全量 `-n 4` 下必然超预算。死锁检测本身仍有效（真死锁
    会在预算内不返回），此处仅为消除负载敏感误报 → verify.ps1 独立分组承载。
    """
    result = subprocess.run(
        [sys.executable, "-c", _CONCURRENT_SCENARIO],
        capture_output=True,
        cwd=str(_REPO),
        timeout=20,
    )
    assert result.returncode == 0, f"并发 robots 拉取子进程失败：{result.stderr!r}"
    assert b"DONE" in result.stdout


def test_contract_robots_cache_hit_skips_fetch() -> None:
    """契约（缓存语义不变式）：缓存命中不得再发起 fetch（并发修复不得退化为
    每次拉取），同 host 第二次调用必须走缓存。"""
    from bandi.tools.web import _robots

    calls: list[str] = []

    async def counting_fetch(client: Any, robots_url: str) -> RobotFileParser:
        calls.append(robots_url)
        parser = RobotFileParser()
        parser.parse(["User-agent: *", "Allow: /"])
        return parser

    async def scenario() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=""))
        async with httpx.AsyncClient(transport=transport) as client:
            await _robots.assert_fetch_allowed(client, "https://host-a.example/page")
            await _robots.assert_fetch_allowed(client, "https://host-a.example/other")
            assert len(calls) == 1, "同 host 第二次调用必须命中缓存"

    _robots.clear_robots_cache()
    with patch.object(_robots, "_fetch_robots", counting_fetch):
        asyncio.run(scenario())
    assert calls and calls[0].endswith("host-a.example/robots.txt")
