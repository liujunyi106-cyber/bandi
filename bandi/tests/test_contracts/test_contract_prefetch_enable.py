from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse


@pytest.mark.parametrize(
    ("field", "expected"),
    [("enabled", True), ("timeout_secs", 1.0), ("max_chars", 4000)],
)
def test_prefetch_config_defaults(field: str, expected: Any) -> None:
    from bandi.core.config import PrefetchConfig

    cfg = PrefetchConfig()
    assert getattr(cfg, field) == expected, f"PrefetchConfig.{field} 默认值回退"


class _RecordingProvider:
    model_name = "mock"
    protocol = "openai"

    def __init__(self) -> None:
        self.seen_messages: list[list[dict[str, Any]]] = []
        self._calls = 0

    def add_response(self, resp: LLMResponse) -> None:
        self._calls += 1
        self._resp = resp

    async def complete(
        self, messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return self._resp

    async def stream(self, messages: list, tools: Any = None, **kwargs: Any) -> Any:
        self.seen_messages.append(messages)
        yield {"type": "content_delta", "content_delta": self._resp.content}

    async def close(self) -> None:
        return None


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_enabled_prefetch_no_duplicate_lines(home: Any, monkeypatch: Any) -> None:
    """
    且 turn 正常完成（超时兜底不阻塞）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse
    from bandi.memory.session.store import SessionStore

    async def _direct_call_model(ctx: Any, messages: list) -> LLMResponse:
        return await ctx.provider.complete(messages, tools=ctx.active_tools)

    monkeypatch.setattr("bandi.agent.deps._production_call_model", _direct_call_model)
    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(title="t")
    provider = _RecordingProvider()
    long_reply = "这是一段足够长的第一轮回复内容，用于触发长期记忆写入通道，" * 6
    provider.add_response(LLMResponse(content=long_reply))
    provider.add_response(LLMResponse(content="第二轮回答"))

    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        r1 = await run_agent_turn(
            user_input="记住项目背景：alpha", session_id=sid, session_store=store
        )
        r2 = await run_agent_turn(
            user_input="alpha 项目背景再讲讲", session_id=sid, session_store=store
        )
    assert r1.reply and r2.reply

    second = provider.seen_messages[-1]
    system_texts = [m.get("content", "") for m in second if m.get("role") == "system"]
    blocks = [c for c in system_texts if "## Runtime Prefetch" in c]
    if blocks:
        other_blob = "\n".join(c for c in system_texts if c not in blocks)
        for line in blocks[0].splitlines():
            key = line.strip()[:80]
            if not key or line.strip().startswith("## Runtime Prefetch"):
                continue
            assert key not in other_blob, f"prefetch 行与既有注入重复: {line[:60]}"
    store.close()


def _make_ctx(turn_index: int, handle: Any) -> Any:
    """最小 ExecutionContext 形状（_inject_prefetch 只消费这几个面）。"""

    class _PrefetchCfg:
        enabled = True
        timeout_secs = 0.05

    class _BandiConfig:
        prefetch = _PrefetchCfg()

    class _Bus:
        async def emit(self, event: Any) -> None:
            return None

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.turn_index = turn_index
    ctx.meta = {"prefetch_handle": handle}
    ctx.bus = _Bus()
    return ctx, _BandiConfig()


class _StubHandle:
    def __init__(self, payload: dict[str, str]) -> None:
        self._payload = payload

    async def consume(self, timeout: float = 1.0) -> dict[str, str]:
        return self._payload


@pytest.mark.asyncio
async def test_inject_prefetch_mechanism() -> None:
    """
    turn_index<2 → 不注入。"""
    from bandi.agent.states import _inject_prefetch

    payload = {"focused_memory": "全新增量记忆行-alpha-beta"}
    base = [
        {"role": "system", "content": "身份 prompt"},
        {"role": "user", "content": "hi"},
    ]

    ctx, cfg = _make_ctx(turn_index=2, handle=_StubHandle(dict(payload)))
    out = await _inject_prefetch(ctx, [dict(m) for m in base], cfg)
    assert any("## Runtime Prefetch" in (m.get("content") or "") for m in out), (
        "第二轮必须注入 prefetch 块"
    )
    block_idx = next(
        i for i, m in enumerate(out) if "## Runtime Prefetch" in (m.get("content") or "")
    )
    assert all(m.get("role") == "system" for m in out[:block_idx]), (
        "块必须在 system 区末尾、首条非 system 消息之前"
    )

    ctx1, cfg1 = _make_ctx(turn_index=1, handle=_StubHandle(dict(payload)))
    out1 = await _inject_prefetch(ctx1, [dict(m) for m in base], cfg1)
    assert not any("## Runtime Prefetch" in (m.get("content") or "") for m in out1), (
        "首轮不注入 prefetch"
    )


@pytest.mark.asyncio
async def test_prefetch_timeout_does_not_block_turn(home: Any) -> None:
    """超时放弃路径：provider 卡死时 consume(timeout) 返回空 payload，"""
    import asyncio
    import time as _time

    from bandi.perception.prefetch import MemoryPrefetchProvider, PrefetchHandle

    class _StuckMemory:
        def search_unified(self, *a: Any, **k: Any) -> list:
            _time.sleep(30)
            return []

    provider_impl = MemoryPrefetchProvider(_StuckMemory())
    handle = PrefetchHandle(provider_impl, "q", "sid", [])
    payload = await asyncio.wait_for(handle.consume(timeout=0.05), timeout=2.0)
    assert payload == {}, "超时必须返回空 payload 不阻塞"
