"""

行为契约：LLM 评估异常时返回 ESCALATE（fail-closed）
而非 REWORK（fail-open，允许坏质量输出绕过闸门）。
"""

from __future__ import annotations

import pytest

from bandi.company.gate_harness import GateHarness, GateVerdict


class _FailingLLM:
    """模拟 LLM 服务异常的 mock。"""

    async def complete(self, messages: list[dict]) -> None:  # type: ignore[return]
        raise RuntimeError("LLM service unavailable")


class _ValueErrorLLM:
    """模拟 LLM 返回格式错误的 mock。"""

    async def complete(self, messages: list[dict]) -> None:  # type: ignore[return]
        raise ValueError("malformed response")


class _AttrErrorLLM:
    """模拟 LLM 属性缺失的 mock。"""

    async def complete(self, messages: list[dict]) -> None:  # type: ignore[return]
        raise AttributeError("missing content attribute")


@pytest.mark.asyncio
async def test_contract_gate_llm_runtime_error_fails_closed() -> None:
    """契约：LLM 抛 RuntimeError → ESCALATE（不再 REWORK）。"""
    harness = GateHarness()
    result = await harness.evaluate_llm("wi-1", "do X", "done X", _FailingLLM())
    assert result.verdict == GateVerdict.ESCALATE, (
        f"LLM 异常应 fail-closed(ESCALATE)，实际返回 {result.verdict}；"
        "LLM 不可用时不应要求重试（REWORK）"
    )


@pytest.mark.asyncio
async def test_contract_gate_llm_value_error_fails_closed() -> None:
    """契约：LLM 抛 ValueError → ESCALATE。"""
    harness = GateHarness()
    result = await harness.evaluate_llm("wi-2", "do X", "done X", _ValueErrorLLM())
    assert result.verdict == GateVerdict.ESCALATE


@pytest.mark.asyncio
async def test_contract_gate_llm_attr_error_fails_closed() -> None:
    """契约：LLM 抛 AttributeError → ESCALATE。"""
    harness = GateHarness()
    result = await harness.evaluate_llm("wi-3", "do X", "done X", _AttrErrorLLM())
    assert result.verdict == GateVerdict.ESCALATE


@pytest.mark.asyncio
async def test_contract_gate_llm_normal_still_passes() -> None:
    """契约：正常 LLM 响应路径不受影响。"""

    class _PassLLM:
        class _Resp:
            content = "PASS: all good"

        async def complete(self, messages: list[dict]):  # type: ignore[return]
            return self._Resp()

    harness = GateHarness()
    result = await harness.evaluate_llm("wi-4", "do X", "done X", _PassLLM())
    assert result.verdict == GateVerdict.PASS
