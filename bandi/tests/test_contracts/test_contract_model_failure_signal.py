"""功能：模型不可用时必须给可读文案 + 独立信号，不得泄漏内部异常串。

真机缺陷（三条并存，都出自 L1 评测）：

① `states.py` 的 `ctx.final_message` 把 `last_llm_error` 直接拼进正文：
   「LLM returned empty response (last_llm_error: ProviderChainExhaustedError:
   All connection attempts failed)」——用户看不懂，且它非空、不超时，
   容易被评测当成一次成功回复。

② `loop.py` 的 `_user_facing_failure` 已把 `LLMUnavailableError` 转成中文，
   但界面上没有独立信号，用户只看到一段正文，不知道要不要继续等。

③ 三处 `MODEL_ERROR` 分支此前只设 `terminal_reason + final_message`，
   不发任何事件——前端拿不到「连接失败」这一信息。
"""

from __future__ import annotations

import asyncio

import pytest

from bandi.agent.loop import _notify_model_failure, _user_facing_failure
from bandi.core.events import EventType
from bandi.core.exceptions import LLMUnavailableError

_LEAK_MARKERS = (
    "LLM call failed",
    "ProviderChainExhaustedError",
    "candidates disabled",
    "last_llm_error",
    "All connection attempts failed",
)


class _Bus:
    """功能：记录 emit 到的事件（不接真实 EventBus）。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


class _Ctx:
    def __init__(self, bus: object) -> None:
        self.bus = bus
        self.session_id = "sess-contract"


def test_user_facing_failure_leaks_nothing() -> None:
    """功能：用户文案不得含任何内部异常串。"""
    text = _user_facing_failure(
        LLMUnavailableError(
            "LLM call failed (candidates disabled): "
            "last_llm_error=ProviderChainExhaustedError: All connection attempts failed"
        )
    )
    for marker in _LEAK_MARKERS:
        assert marker not in text, f"内部串泄漏：{marker}"
    assert text.strip(), "必须给用户一句可读说明"


def test_notify_emits_llm_error_for_unavailable() -> None:
    """功能：模型不可用必须发 LLM_ERROR 事件（前端才有独立信号）。"""
    bus = _Bus()
    asyncio.run(_notify_model_failure(_Ctx(bus), LLMUnavailableError("boom")))

    assert [e.type for e in bus.events] == [EventType.LLM_ERROR]
    data = bus.events[0].data
    assert data.get("reason") == "model_unavailable"
    assert data.get("session_id") == "sess-contract"
    assert data.get("message"), "事件必须带可读文案"
    for marker in _LEAK_MARKERS:
        assert marker not in str(data.get("message")), f"事件文案泄漏内部串：{marker}"


def test_notify_ignores_other_exceptions() -> None:
    """功能：非模型不可用的异常不得冒充（避免误报打断正常失败路径）。"""
    bus = _Bus()
    asyncio.run(_notify_model_failure(_Ctx(bus), ValueError("other")))
    assert bus.events == []


def test_notify_survives_missing_bus() -> None:
    """功能：ctx 无 bus / bus 无 emit 时不得抛（通知失败不能覆盖原始终态）。"""

    class _NoBus:
        session_id = "x"

    asyncio.run(_notify_model_failure(_NoBus(), LLMUnavailableError("boom")))


def test_states_empty_response_message_is_readable() -> None:
    """功能：空响应文案不得拼 last_llm_error（第二条泄漏路径）。"""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "bandi" / "agent" / "states.py"
    text = source.read_text(encoding="utf-8")
    # 定位「LLM returned empty response」那一段（若已被删除则本断言直接通过）
    if "LLM returned empty response" not in text:
        return
    marker = "LLM returned empty response"
    start = text.index(marker)
    window = text[start : start + 240]
    assert "last_llm_error" not in window, (
        f"空响应文案仍拼内部错误串：{window[:160]!r}"
    )


if __name__ == "__main__":  # pragma: no cover - 手工探针
    pytest.main([__file__, "-q"])

def test_all_model_error_branches_are_wired() -> None:
    """功能：三处 MODEL_ERROR 分支都必须接上通知（防只接一处）。

    真机结构：`loop.py` 有 TERMINATE / executing-RETRY / 无 recovery_manager
    三条路径都会置 MODEL_ERROR；只接一条等于另两条用户仍看不到信号。
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "bandi" / "agent" / "loop.py"
    text = source.read_text(encoding="utf-8")
    assigns = text.count("ctx.final_message = _user_facing_failure(e)")
    wired = text.count("await _notify_model_failure(ctx, e)")
    assert assigns >= 3, f"预期 >=3 处 final_message 赋值，实际 {assigns}"
    assert wired == assigns, (
        f"每个 MODEL_ERROR 分支都要发通知：赋值 {assigns} 次，"
        f"已接线 {wired} 次"
    )
