# 功能：幻觉检测/内容过滤/Verifier 链路验证

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _RecordingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


@pytest.fixture
def home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_content_filter_blocks_unsafe_output(home: Any) -> None:
    """内容过滤必须真实拦截不安全输出（终回答被替换为拦截消息）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = MockProvider()
    provider.add_response(LLMResponse(content="我要自杀"))
    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(
            user_input="hi",
            session_id=sid,
            session_store=store,
        )
    assert result.reply
    assert "安全护栏" in result.reply or "拦截" in result.reply, (
        f"不安全输出必须被拦截替换，实际 {result.reply!r}"
    )
    store.close()


@pytest.mark.asyncio
async def test_hallucination_checker_wired(home: Any) -> None:
    """幻觉检测必须真实挂载（safety.enabled=True 时 ctx 有 hallucination_checker）。"""
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.llm.api_key = ""
    ctx = build_agent_context(user_input="test", bandi_config=cfg)
    hc = getattr(ctx, "hallucination_checker", None)
    assert hc is not None, "safety.enabled 时 hallucination_checker 必须注入 ctx"
    assert hasattr(hc, "check"), "checker 必须可调用"


@pytest.mark.asyncio
async def test_verifier_wired_when_safety_enabled(home: Any) -> None:
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig

    cfg = BandiConfig()
    cfg.llm.api_key = ""
    ctx = build_agent_context(user_input="test", bandi_config=cfg)
    verifier = getattr(ctx, "verifier", None)
    assert verifier is not None, "safety.enabled 时 Verifier 必须注入 ctx（验证闸门生效）"
    assert verifier.policy.enabled is True
    assert verifier.policy.fail_open is False, "fail-open 拍板为不放行"
