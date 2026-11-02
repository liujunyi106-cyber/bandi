# 功能：契约——reasoning-only 重试的直连模式（不做模型候选链）
# 背景：换模型候选重试会掩盖主模型错误——直连模式只重发当前模型。
# 钉行为：
# ① 链配置被忽略——重试 = 同模型保底重发一次（直用 ctx.provider 现实例）；
# ② 保底重发不经 resolver（激活档案洗回端点/凭证 → Bearer 空 key 连败）；
# ③ 破坏者：链存在其他模型也不得触发 resolver（回归守卫）。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.agent import states as states_mod
from bandi.agent.base import ExecutionContext
from bandi.core.config import AgentConfig
from bandi.core.message import LLMResponse, Message, MessageRole


class _FakeProvider:
    def __init__(self, model_name: str, reply: str) -> None:
        self.model_name = model_name
        self._reply = reply
        self.calls = 0

    async def complete(self, messages: list[dict], tools: Any = None, **kw: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content=self._reply, finish_reason="stop")


class _FakeRegistry:
    def __init__(self, provider: _FakeProvider) -> None:
        self._provider = provider
        self.resolved_models: list[str] = []

    def get_provider_for_model(self, model: str, cfg: Any) -> _FakeProvider:
        self.resolved_models.append(model)
        return self._provider


def _ctx(provider: _FakeProvider) -> ExecutionContext:
    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=provider,  # type: ignore[arg-type]
        config=AgentConfig(),
        bus=None,
    )
    return ctx


def _bandi_config(chain: list[str]) -> Any:
    return SimpleNamespace(llm=SimpleNamespace(fallback_chain=chain, timeout=5))


@pytest.mark.asyncio
async def test_retry_ignores_bandi_config_chain_direct_mode() -> None:
    """①③ 链配置被忽略（候选通道已删）——重试=同模型保底重发一次。"""
    current = _FakeProvider("deepseek-flash", "retry-ok")
    retry = _FakeProvider("hub-flash", "should-not-use")
    registry = _FakeRegistry(retry)
    ctx = _ctx(current)

    out = await states_mod._retry_reasoning_only(
        ctx,
        [{"role": "user", "content": "hi"}],
        allow_stream=False,
        registry=registry,
        bandi_config=_bandi_config(["hub-flash", "xiaomi/mimo-v2.6-flash"]),
    )

    assert out is not None and out.content == "retry-ok"
    assert registry.resolved_models == [], "候选链已删——不得经 resolver 换模型"
    assert current.calls == 1, "同模型保底重发一次"


@pytest.mark.asyncio
async def test_fallback_same_model_reuses_current_provider() -> None:
    """② 保底重发（链为空/无其他候选）：直用 ctx.provider——不经 resolver 重解析。"""
    current = _FakeProvider("deepseek-flash", "retry-ok")
    registry = _FakeRegistry(_FakeProvider("deepseek-flash", "should-not-use"))
    ctx = _ctx(current)

    out = await states_mod._retry_reasoning_only(
        ctx,
        [{"role": "user", "content": "hi"}],
        allow_stream=False,
        registry=registry,
        bandi_config=_bandi_config([]),
    )

    assert out is not None and out.content == "retry-ok"
    assert current.calls == 1, "保底重发必须直用当前 provider 实例"
    assert registry.resolved_models == [], (
        "保底重发不得经 resolver（激活档案洗回端点/凭证 → Bearer 空 key 连败）"
    )


@pytest.mark.asyncio
async def test_retry_uses_current_model_even_when_chain_lists_others() -> None:
    """破坏者：链列出其他模型也被忽略——仍同模型重发（不得触发 resolver）。"""
    current = _FakeProvider("deepseek-flash", "retry-ok")
    registry = _FakeRegistry(_FakeProvider("hub-flash", "should-not-use"))
    ctx = _ctx(current)

    out = await states_mod._retry_reasoning_only(
        ctx,
        [{"role": "user", "content": "hi"}],
        allow_stream=False,
        registry=registry,
        bandi_config=_bandi_config(["deepseek-flash", "hub-flash"]),
    )

    assert out is not None and out.content == "retry-ok"
    assert registry.resolved_models == [], "链已删——任何链内容都不得换模型"
    assert current.calls == 1
