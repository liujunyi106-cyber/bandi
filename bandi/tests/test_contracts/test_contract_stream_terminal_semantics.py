# 「仅思维链」与「真空回复」的区分
#
# LLM_STREAM_EVENT thinking_delta="2" → done(stop) → usage.completion_tokens=3，content 通道为空
# 「LLM returned empty response (last_llm_error: unknown)」——归因失真且不可行动。
#
# ② sse_stream **无条件收尾**：自然关闭且无 finish_reason ⇒ DONE("incomplete")
# 不得零事件关闭伪装成正常 stop；
# ③ merge_stream_events：无 DONE ⇒ "incomplete"（与 states.py 的 seen_done 语义一致）；
# ④ 端到端（生产主链路 run_agent_turn）：「仅思维链」与「真空回复」**分开归因**。
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from bandi.core.message import LLMResponse, TokenUsage
from bandi.llm.adapters.openai_compat import sse_stream
from bandi.llm.streaming import StreamEvent, StreamEventType, merge_stream_events

# ── 假 httpx 客户端：把预设 SSE 字节流交给 sse_stream（不出网） ───────────────


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self._body


class _FakeStreamCtx:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse(self._body)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(
        self, method: str, url: str, json: Any = None, headers: Any = None
    ) -> _FakeStreamCtx:
        return _FakeStreamCtx(self._body)


def _collect(body: bytes) -> list[StreamEvent]:
    async def _gen() -> AsyncIterator[StreamEvent]:
        async for ev in sse_stream(_FakeClient(body), "http://x/v1/chat/completions", {}, {}):
            yield ev

    return asyncio.run(_collect_gen(_gen()))


async def _collect_gen(gen: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [ev async for ev in gen]


def _content_of(events: list[StreamEvent]) -> str:
    return "".join(
        ev.content_delta or "" for ev in events if ev.type is StreamEventType.CONTENT_DELTA
    )


def _done(events: list[StreamEvent]) -> StreamEvent:
    dones = [ev for ev in events if ev.type is StreamEventType.DONE]
    assert len(dones) == 1, f"应恰有一条 DONE，实际 {len(dones)}"
    return dones[0]


def test_trailing_line_without_newline_is_parsed() -> None:
    """契约①：末行无尾换行（部分端点不合规）⇒ 最后一条 delta/finish 不得丢。

    修复前：只按 "\\n" 切分，循环退出后残余 buffer 被静默丢弃 ⇒ content 只剩 "A"。
    """
    body = (
        b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"B"},"finish_reason":"stop"}]}'
    )
    events = _collect(body)
    assert _content_of(events) == "AB", f"末行被丢弃：{[e.type.name for e in events]}"
    assert _done(events).finish_reason == "stop"


def test_natural_close_without_finish_reason_is_incomplete() -> None:
    """契约②：自然关闭且上游从未给 finish_reason ⇒ DONE("incomplete")。

    修复前：`if finish_reason is not None` 守卫 ⇒ **零事件关闭**
    「断流/上游异常」伪装成正常结束（消费端无从区分）。
    """
    body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    events = _collect(body)
    assert _content_of(events) == "hi"
    assert _done(events).finish_reason == "incomplete"


def test_done_marker_path_unchanged() -> None:
    """契约②（防回归）：规范 [DONE] 路径仍产出 DONE("stop") 并终止。"""
    body = (
        b'data: {"choices":[{"delta":{"content":"A"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )
    events = _collect(body)
    assert _content_of(events) == "A"
    assert _done(events).finish_reason == "stop"


def test_usage_only_chunk_after_finish_still_captured() -> None:
    """契约②（既有行为防回归）：finish 之后的 usage-only chunk 必须进 DONE。"""
    body = (
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    events = _collect(body)
    done = _done(events)
    assert done.finish_reason == "stop"
    assert done.usage is not None and done.usage.prompt_tokens == 10


# ── ③ merge_stream_events：无 DONE ⇒ incomplete（与 states.py 语义一致） ────


def _from_events(events: list[StreamEvent]) -> AsyncIterator[StreamEvent]:
    async def _gen() -> AsyncIterator[StreamEvent]:
        for ev in events:
            yield ev

    return _gen()


def test_merge_without_done_is_incomplete() -> None:
    """契约③：流结束但从未收到 DONE ⇒ finish_reason="incomplete"（此前伪造 "stop"）。"""
    response = asyncio.run(
        merge_stream_events(
            _from_events([StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="hi")])
        )
    )
    assert response.finish_reason == "incomplete"
    assert response.content == "hi"


def test_merge_done_reason_passthrough() -> None:
    """契约③（防回归）：收到 DONE ⇒ finish_reason 原样透传。"""
    response = asyncio.run(
        merge_stream_events(
            _from_events(
                [
                    StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="A"),
                    StreamEvent(type=StreamEventType.DONE, finish_reason="stop"),
                ]
            )
        )
    )
    assert response.finish_reason == "stop"
    assert response.content == "A"


# ── ④ 端到端：「仅思维链」与「真空回复」分开归因（生产主链路 run_agent_turn） ──


class _ReasoningOnlyProvider:
    """complete 恒返回「仅思维链、正文空」的 provider 桩。"""

    @property
    def model_name(self) -> str:
        return "reasoning-only-stub"

    @property
    def protocol(self) -> str:
        return "openai"

    @property
    def supports_streaming(self) -> bool:
        return False

    async def complete(self, messages: list, tools: Any = None, **kwargs: Any):
        return LLMResponse(
            content="", reasoning="思考链内容", tool_calls=None, finish_reason="stop"
        )

    async def stream(self, messages: list, tools: Any = None, **kwargs: Any):
        yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    async def close(self) -> None:
        pass


class _PlainEmptyProvider(_ReasoningOnlyProvider):
    async def complete(self, messages: list, tools: Any = None, **kwargs: Any):
        return LLMResponse(content="", tool_calls=None, finish_reason="stop")


async def _run_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: type) -> str:
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    monkeypatch.setattr("bandi.core.agent_runner.build_provider", lambda cfg: provider())
    # 候选 disposition：主调用经 ProviderRegistry 解析（registry.get_provider）——
    # 桩 provider 不在模型目录，测试直通注入同一实例
    monkeypatch.setattr(
        "bandi.llm.registry.ProviderRegistry.get_provider",
        lambda self, model, config, **kwargs: provider(),
    )
    result = await run_agent_turn(
        user_input="1+1等于几？只回答数字",
        session_store=SessionStore(str(Path(tmp_path) / "s.db")),
        project_root=str(Path(tmp_path) / "proj"),
    )
    reason = getattr(result.terminal_reason, "name", result.terminal_reason)
    assert reason == "MODEL_ERROR"
    return result.reply or ""


class TestReasoningOnlyVsPlainEmpty:
    @pytest.mark.asyncio
    async def test_reasoning_only_is_attributed_distinctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """契约④：仅思维链 ⇒ 文案必须**如实区分**（含 reasoning 体量与可行动指引）
        不得复用「empty response / unknown」掩盖真实根因。"""
        reply = await _run_turn(monkeypatch, tmp_path, _ReasoningOnlyProvider)
        assert "思维链" in reply, f"归因未区分仅思维链：{reply[:120]!r}"
        assert "unknown" not in reply, "不得再报 last_llm_error=unknown（根因已实证）"

    @pytest.mark.asyncio
    async def test_plain_empty_keeps_original_semantics(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """契约④（防回归）：真空回复（无 reasoning）仍走原「empty response」语义。"""
        reply = await _run_turn(monkeypatch, tmp_path, _PlainEmptyProvider)
        assert "LLM returned empty response" in reply, f"真空回复语义被破坏：{reply[:120]!r}"


def test_usage_defaults_present() -> None:
    """前提守卫：TokenUsage 可零参构造（merge 的空流兜底依赖）。"""
    usage = TokenUsage(prompt_tokens=0, completion_tokens=0)
    assert usage.prompt_tokens == 0 and usage.completion_tokens == 0
    assert json.dumps({"ok": True})  # 占位断言保持 import 有意义
