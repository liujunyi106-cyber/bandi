# 功能：契约——子代理批内闸门 + 批内闸门跨执行器共享（并发工程复核 🟡-3）
#
# ① `agent/subagent.py:553` 构造子代理 `ToolExecutor` 时**漏传 `max_concurrency`**
# （同函数 `:542` 已持有 `_tools_cfg`，`:573-579` 还从它取了 7 个字段）→ 子代理
# 批内无限并发，的闸门被绕过；
# ② `tools/executor.py:357` 的 semaphore 是**每批新建**的 → 连父 Agent 都不是
# 「全局闸门」，只是「批内上限」：父批 + 每个子代理批 + 每个员工会话批各计一份
# 组合峰值 = 批数 × 上限。承诺的「复用同一全局闸门」两层都没兑现。
#
# 本契约钉**行为**：
# 1) 子代理上限取值与主链 config 同源（单一取值点）
# 2) 同一事件循环内闸门被复用（跨执行器共享）
# 3) **全局**并发峰值 ≤ 上限（不因多个执行器而翻倍）
# 4) `None`（不限）语义与显式 `0`（主动开闸门）透传语义不变
# 5) 防自锁护栏：嵌套执行路径的子代理工具必须留在串行分区
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.agent.subagent import (
    DEFAULT_MAX_CONCURRENCY,
    subagent_batch_concurrency,
)
from bandi.core.config import ToolsConfig
from bandi.core.message import ToolCall
from bandi.tools.base import BaseTool
from bandi.tools.executor import ToolExecutor, shared_concurrency_limiter
from bandi.tools.registry import ToolRegistry

_LIMIT = 4


class _ProbeTool(BaseTool):
    """并发探针：统计**跨执行器**的同时在跑峰值（同一实例被多个执行器复用）。"""

    name = "concurrency_probe"
    description = "measure in-flight concurrency"
    is_concurrency_safe = True
    is_read_only = True
    dedup_scope = "none"
    parameters: dict = {"type": "object", "additionalProperties": False, "properties": {}}

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self._guard = asyncio.Lock()

    async def execute(self, params: dict, context: object | None = None) -> str:
        async with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.05)
        finally:
            async with self._guard:
                self.active -= 1
        return "ok"


def _executor(probe: _ProbeTool, *, max_concurrency: Any) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register_instance(probe)
    return ToolExecutor(registry=registry, max_concurrency=max_concurrency)


def _calls(n: int, prefix: str) -> list[ToolCall]:
    return [ToolCall(id=f"{prefix}{i}", name="concurrency_probe", arguments={}) for i in range(n)]


# ---------------------------------------------------------------------------
# 1) 取值单一源
# ---------------------------------------------------------------------------


def test_subagent_cap_comes_from_tools_config() -> None:
    """契约 1：子代理批内上限与 `ToolsConfig.max_concurrency` 同源（不再漏传）。"""
    cfg = ToolsConfig()
    assert cfg.max_concurrency == 8
    assert subagent_batch_concurrency(cfg) == cfg.max_concurrency


def test_subagent_cap_falls_back_when_config_field_missing() -> None:
    """配置缺字段（旧形态/None）→ 回落默认上限，不得退化成「不限」。"""
    assert subagent_batch_concurrency(object()) == DEFAULT_MAX_CONCURRENCY
    assert subagent_batch_concurrency(None) == DEFAULT_MAX_CONCURRENCY


def test_subagent_cap_passes_through_explicit_zero() -> None:
    """契约 4：显式 0 = 主动关闸门，必须原样透传（由执行器告警，不得静默改回默认）。"""
    assert subagent_batch_concurrency(ToolsConfig(max_concurrency=0)) == 0


# ---------------------------------------------------------------------------
# 2) 闸门共享（机制）
# ---------------------------------------------------------------------------


def test_gate_is_reused_within_same_loop() -> None:
    """契约 2：同 loop 同上限 → 同一 semaphore 实例（原实现每批新建）。"""

    async def _probe() -> tuple[Any, Any, Any]:
        first = shared_concurrency_limiter(_LIMIT)
        second = shared_concurrency_limiter(_LIMIT)
        other = shared_concurrency_limiter(_LIMIT + 1)
        return first, second, other

    first, second, other = asyncio.run(_probe())
    assert first is second, "同上限必须复用同一闸门"
    assert first is not other, "不同上限必须是不同闸门"


# ---------------------------------------------------------------------------
# 3) 全局峰值（核心行为）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_executors_share_one_gate_globally() -> None:
    """**核心契约**：两个执行器（模拟父 / 子代理）各跑一批 → 合计不得超过上限。

    原实现每批新建 semaphore → 两批各 4 → 全局峰值 8（闸门只限单批，乘积未收敛）。
    """
    probe = _ProbeTool()
    parent = _executor(probe, max_concurrency=_LIMIT)
    child = _executor(probe, max_concurrency=_LIMIT)

    await asyncio.gather(
        parent.execute(_calls(_LIMIT, "p")),
        child.execute(_calls(_LIMIT, "c")),
    )

    assert probe.peak <= _LIMIT, f"全局峰值 {probe.peak} 超过闸门 {_LIMIT}（闸门未跨执行器共享）"


@pytest.mark.asyncio
async def test_single_executor_respects_cap() -> None:
    """回归：单执行器批内峰值仍受上限约束。"""
    probe = _ProbeTool()
    ex = _executor(probe, max_concurrency=2)

    await ex.execute(_calls(6, "s"))

    assert probe.peak <= 2


@pytest.mark.asyncio
async def test_unlimited_still_unbounded() -> None:
    """契约 4 回归：`None`（不限）语义保持不变。"""
    probe = _ProbeTool()
    ex = _executor(probe, max_concurrency=None)

    await ex.execute(_calls(6, "u"))

    assert probe.peak > _LIMIT, "None 应保持不限（全部并发）"


# ---------------------------------------------------------------------------
# 4) 防自锁护栏
# ---------------------------------------------------------------------------


def test_subagent_spawn_stays_in_serial_partition() -> None:
    """契约 5：共用闸门的前提——嵌套执行路径必须留在**串行分区**。

    `subagent_spawn` 若变为 concurrency-safe，父子共用闸门时会自锁
    （父批占满槽等子代理，子代理批等槽）——此断言是防回归护栏。
    """
    from bandi.agent.subagent_tool import SubAgentTool

    assert SubAgentTool.is_concurrency_safe is False, (
        "subagent_spawn 一旦参与并发分区，父子共享闸门将自锁——须同步改闸门为可重入"
    )
