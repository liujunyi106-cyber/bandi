"""功能：MCP 重连必须有超时上界，绝不能拖死 Agent 主循环。

真机缺陷（一个「搜索 AI 新闻」任务耗时 31m55s 却只发生 3 次 LLM 调用）：

    02:08:37  llm call #2
    02:11:42  memory consolidation            (+167s)
    02:27:42  Health check: MCP client bandi unavailable, reconnecting   (+959s)
    02:27:44  reconnected
    02:39:21  llm call #3                     (+697s)

该区间**没有任何外网连接**（不是在等模型），进程 CPU 5 秒只涨 0.09s
（也不是死循环）——纯粹是健康检查循环被挂住的 await 阻塞。

根因链：
- `StdioTransport.connect()` 裸 await 拉子进程，无超时；
- `MCPClient.reconnect()` 内部 dispose + connect + initialize 握手，无超时；
- `MCPBroker._health_check_loop()` 直接 await 上面两步，无超时；
三者共享会话事件循环 ⇒ 任何一步挂住，下一轮 LLM 调用永远排不到。

契约钉住：三个层级都必须在有限时间内返回（失败也比悬挂好）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from bandi.tools import mcp_broker as broker_mod
from bandi.tools import mcp_client as client_mod
from bandi.tools.mcp_broker import MCPBroker
from bandi.tools.mcp_client import StdioTransport


def test_spawn_timeout_is_bounded() -> None:
    """功能：子进程拉起必须有秒级上界（npm server 冷启动可拖到分钟级）。"""
    assert 0 < client_mod._SPAWN_TIMEOUT_SEC <= 120
    transport = StdioTransport("no-such-binary", spawn_timeout_sec=0.2)
    assert transport._spawn_timeout_sec == 0.2


def test_hung_spawn_raises_instead_of_hanging() -> None:
    """功能：子进程拉不起来时必须在超时内抛错，不得无限 await。"""

    async def _never_spawns(*_a: object, **_kw: object) -> None:
        await asyncio.sleep(3600)

    original = client_mod.spawn_managed_subprocess
    client_mod.spawn_managed_subprocess = _never_spawns
    try:
        transport = StdioTransport("hung-server", spawn_timeout_sec=0.3)
        started = time.monotonic()
        with pytest.raises(client_mod.MCPError):
            asyncio.run(transport.connect())
        elapsed = time.monotonic() - started
    finally:
        client_mod.spawn_managed_subprocess = original
    assert elapsed < 5, f"超时保护未生效，实际等了 {elapsed:.1f}s"


def test_broker_reconnect_timeout_is_bounded() -> None:
    """功能：单次重连（含工具重注册）必须有上界，默认 60s 而非无限。"""
    broker = MCPBroker()
    assert 0 < broker._reconnect_timeout_sec <= 300
    assert broker_mod._RECONNECT_TIMEOUT_SEC == broker._reconnect_timeout_sec


def test_health_loop_skips_hung_client() -> None:
    """功能：某个 client 重连挂住时，健康循环必须跳过它继续下一轮。

    这是本契约的核心：旧实现直接 `await client.reconnect()`，一次挂住就让
    整个循环停摆——界面表现为「正在执行」几十分钟、后端零进展。
    """

    class _HungClient:
        name = "hung"

        def __init__(self) -> None:
            self.attempted = 0

        @property
        def available(self) -> bool:
            return False

        async def reconnect(self) -> bool:
            self.attempted += 1
            await asyncio.sleep(3600)
            return True

    async def _run() -> int:
        broker = MCPBroker(health_check_interval=0.05, reconnect_timeout_sec=0.2)
        hung = _HungClient()
        broker._pools = {"p": [hung]}  # type: ignore[assignment]
        broker._running = True
        task = asyncio.create_task(broker._health_check_loop())
        await asyncio.sleep(1.0)
        broker._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return hung.attempted

    attempted = asyncio.run(_run())
    assert attempted >= 2, (
        f"挂住的 client 只被尝试 {attempted} 次：超时后循环必须继续下一轮，"
        "而不是卡在这一轮"
    )


def test_healthy_client_is_not_reconnected() -> None:
    """功能：可用 client 不得被无谓重连（防超时改造引入抖动）。"""

    class _HealthyClient:
        name = "ok"
        attempted = 0

        @property
        def available(self) -> bool:
            return True

        async def reconnect(self) -> bool:
            type(self).attempted += 1
            return True

    async def _run() -> None:
        broker = MCPBroker(health_check_interval=0.05, reconnect_timeout_sec=0.2)
        broker._pools = {"p": [_HealthyClient()]}  # type: ignore[assignment]
        broker._running = True
        task = asyncio.create_task(broker._health_check_loop())
        await asyncio.sleep(0.6)
        broker._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert _HealthyClient.attempted == 0
