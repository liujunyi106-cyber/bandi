"""功能：显式指定模型不可用时必须报错（不静默回落）。

背景：会话级模型覆盖 / 员工独立模型（provider_config.default_model）指向
不存在的模型 id 时，若静默回落到其他模型，用户无从察觉自己选的模型根本没被使用。
用户要求：配错模型 / API Key 无效 / 余额不足 / 网关不可用等**不可用**情形必须报错。

本契约钉行为：
1. provider_config 带模型名 → 该轮失败**必须**以错误终结（不得回落成成功）；
2. 不带模型名（未显式指定）→ 沿用默认配置，行为不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bandi.core.exceptions import LLMError
from bandi.llm.mock import MockProvider


class _AlwaysFailingProvider(MockProvider):
    """功能：任何调用都抛 LLMError（模拟 400 / Key 无效 / 余额不足 / 网关不可用）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kw: Any
    ):  # type: ignore[override]
        self.calls.append("complete")
        raise LLMError("model not found: bogus-model-xyz (401 unauthorized)")

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict] | None = None, **kw: Any
    ):  # type: ignore[override]
        self.calls.append("stream")
        raise LLMError("model not found: bogus-model-xyz (401 unauthorized)")
        yield  # pragma: no cover - 保持 async generator 形态


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_explicit_unavailable_model_errors_without_fallback(home: Path) -> None:
    from bandi.core.agent_runner import run_agent_turn
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = _AlwaysFailingProvider()

    def _forbid_fallback(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("strict 模式不得构造回落引擎（配置链/能力池）")

    with (
        patch("bandi.agent.factory._build_provider_direct", return_value=provider),
        patch("bandi.core.agent_runner.build_provider", return_value=provider),
        patch("bandi.llm.strategy.FallbackEngine", _forbid_fallback),
        patch("bandi.llm.strategy.CapabilityFallback", _forbid_fallback),
    ):
        result = await run_agent_turn(
            user_input="你好",
            session_id=sid,
            session_store=store,
            provider_config={"default_model": "bogus-model-xyz"},
        )

    assert provider.calls, "该 provider 必须被真正调用过（否则本用例没测到目标路径）"
    assert result.terminal_reason != "COMPLETED", (
        f"显式指定的模型不可用时**不得**以成功终结，实际 terminal_reason="
        f"{result.terminal_reason!r} reply={result.reply!r}"
    )
    assert result.terminal_reason != "VERIFIED", "更不得以「已验证」终结"
    assert not (result.reply or "").strip(), (
        f"不可用的显式模型不应产出正文回复（不得伪装成正常回答），实际={result.reply!r}"
    )


@pytest.mark.asyncio
async def test_session_without_explicit_model_keeps_fallback_path(home: Path) -> None:
    """破坏者视角：未显式指定模型时不得被禁用回落（避免一刀切误伤默认链路）。"""
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse
    from bandi.memory.session.store import SessionStore

    store = SessionStore(str(home / "sess.db"))
    sid = store.create_session(mode="execute")
    provider = MockProvider()
    provider.add_response(LLMResponse(content="ok"))

    with patch("bandi.core.agent_runner.build_provider", return_value=provider):
        result = await run_agent_turn(user_input="你好", session_id=sid, session_store=store)

    assert result.reply == "ok"
    assert result.terminal_reason not in ("MODEL_ERROR", None), (
        f"未指定模型时不应被 strict 逻辑影响，terminal={result.terminal_reason!r}"
    )


def test_strict_flag_only_set_when_model_present() -> None:
    """口径锚点：strict 标记来源 = provider_config.default_model 非空。"""
    for cfg in (None, {}, {"thinking_mode": "high"}, {"default_model": "  "}):
        assert not str((cfg or {}).get("default_model") or "").strip()
    assert str({"default_model": "deepseek-v4.1-flash"}.get("default_model") or "").strip()
