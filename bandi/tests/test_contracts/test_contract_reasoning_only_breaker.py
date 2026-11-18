"""功能：模型持续只输出推理、不给正文时必须快速失败，不能空转到会话时限。

真机缺陷：某条用例里模型反复返回 reasoning-only，重试**不消耗轮次预算**，
于是一路重试到 `_DEFAULT_SESSION_TIMEOUT_SEC`（默认 3600 秒）。界面表现是
「一直转圈、不出结果」，评测里表现为「跑满 600 秒仍是空回复」。

契约钉三件行为：
  ① 连续 reasoning-only 达阈值后不再重试
  ② 中途成功一次就重置连败计数（偶发失败不该累计）
  ③ 熔断后仍返回原响应，由上层决定如何收尾（不能抛异常打断主链路）
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent.states import _guard_reasoning_only, _is_reasoning_only


class _Ctx:
    """功能：够用的执行上下文替身——只需要 meta 与 bus。"""

    def __init__(self) -> None:
        self.meta: dict[str, Any] = {"session_id": "s-1"}
        self.bus = None
        self.turn_index = 0
        self.provider = SimpleNamespace(model_name="m1")
        self.provider_name = "m1"
        self.config = SimpleNamespace(max_iterations=60)
        self.active_tools = None


def _reasoning_only_response() -> Any:
    """功能：造一个只有推理、正文为空的响应。"""
    return SimpleNamespace(
        content="",
        reasoning="我在想……",
        tool_calls=[],
        finish_reason="stop",
        usage=None,
    )


def _normal_response() -> Any:
    """功能：造一个正文正常的响应。"""
    return SimpleNamespace(
        content="这是正文",
        reasoning="",
        tool_calls=[],
        finish_reason="stop",
        usage=None,
    )


def test_reasoning_only_is_detected() -> None:
    """功能：空正文 + 有推理必须被判为 reasoning-only。"""
    assert _is_reasoning_only(_reasoning_only_response(), safety_finish=False) is True
    assert _is_reasoning_only(_normal_response(), safety_finish=False) is False


def test_streak_increments_and_resets(monkeypatch: pytest.MonkeyPatch) -> None:
    """功能：连续失败累计、成功一次即清零。"""
    ctx = _Ctx()

    async def _never_succeeds(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("bandi.agent.states._retry_reasoning_only", _never_succeeds)

    asyncio.run(_guard_reasoning_only(ctx, _reasoning_only_response(), [], False))
    assert ctx.meta["_reasoning_only_streak"] == 1
    asyncio.run(_guard_reasoning_only(ctx, _reasoning_only_response(), [], False))
    assert ctx.meta["_reasoning_only_streak"] == 2

    # 成功一次清零
    asyncio.run(_guard_reasoning_only(ctx, _normal_response(), [], False))
    assert "_reasoning_only_streak" not in ctx.meta


def test_circuit_breaker_stops_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    """功能：超过阈值后不得再触发重试——这是杜绝空转的关键。"""
    ctx = _Ctx()
    calls: list[int] = []

    async def _counted(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        return None

    monkeypatch.setattr("bandi.agent.states._retry_reasoning_only", _counted)

    # 阈值内会重试
    for _ in range(3):
        asyncio.run(_guard_reasoning_only(ctx, _reasoning_only_response(), [], False))
    before = len(calls)

    # 第 4 次（超阈值）不再重试
    result = asyncio.run(
        _guard_reasoning_only(ctx, _reasoning_only_response(), [], False)
    )
    assert len(calls) == before, "熔断后仍在重试，会空转到会话时限"
    assert result is not None, "熔断后必须返回响应，不能抛异常打断主链路"


def test_breaker_returns_original_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """功能：熔断时返回原响应（正文为空），由上层决定收尾方式。"""
    ctx = _Ctx()
    ctx.meta["_reasoning_only_streak"] = 99

    async def _should_not_call(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("已经熔断，不应再调用重试")

    monkeypatch.setattr("bandi.agent.states._retry_reasoning_only", _should_not_call)
    original = _reasoning_only_response()
    result = asyncio.run(_guard_reasoning_only(ctx, original, [], False))
    assert result is original
