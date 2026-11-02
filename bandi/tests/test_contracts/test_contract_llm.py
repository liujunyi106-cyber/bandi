# 功能：LLM 层行为契约（防 复审问题复发）
# 契约原则：候选池全注册+凭证过滤；默认模型非退役；缓存键含 provider；
# 预算跨日自愈；max_tokens 正值。
from __future__ import annotations

import asyncio
import json

import pytest

from bandi.core.config import BandiConfig, LLMConfig
from bandi.llm.capabilities.adapters import from_model_spec
from bandi.llm.model_catalog import BUILTIN_MODELS, ModelCatalog, ModelSpec
from bandi.llm.strategy import ModelRouter

# 已从官方 API 下线的退役模型（代码默认值不得引用）
_RETIRED_MODELS = {"deepseek-chat", "deepseek-reasoner"}


def test_contract_router_pools_all_registered() -> None:
    """契约 1：ModelRouter 三档候选池全部模型必须在 BUILTIN_MODELS 注册"""
    router = ModelRouter()
    for pool in (
        router.select_pool(100, 0),
        router.select_pool(2000, 4),
        router.select_pool(10000, 10),
    ):
        for model in pool:
            assert model in BUILTIN_MODELS, f"候选池含未注册模型 {model}"


def test_contract_default_model_not_retired() -> None:
    """契约 2：代码默认模型不得引用已退役模型（防 deepseek-chat 默认值复发）。"""
    cfg = LLMConfig()
    assert cfg.default_model not in _RETIRED_MODELS, (
        f"默认模型 {cfg.default_model} 已从官方 API 下线"
    )
    for m in cfg.fallback_chain:
        assert m not in _RETIRED_MODELS, f"降级链 {m} 已下线"


def test_contract_availability_filters_unconfigured(monkeypatch, tmp_path) -> None:
    """契约 3：未配凭证的 provider 模型 availability 必须 False（防候选链
    逐个网络级失败复发）。行为断言：env 与 config 均未配 key 时 openai_compat
    """
    for key in ("BANDI_LLM_API_KEY", "OPENAI_API_KEY", "BANDI_LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    spec = ModelSpec(name="custom-gateway", provider="openai_compat", context_window=32_000)
    assert from_model_spec(spec).available() is False, "未配凭证不可用"


def test_contract_availability_uses_config_credentials(monkeypatch, tmp_path) -> None:
    """契约 3b（机制修复）：config 配了 api_key+base_url 的 openai_compat 模型
    旧契约只测 env 删除路径，把「只看 env」的实现细节钉成了契约。"""
    import yaml

    from bandi.core.config import BandiConfig

    home = tmp_path
    cfg_dir = home / ".bandi"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "api_key": "test-key",
                    "base_url": "https://test.example.com/v1",
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    for key in ("BANDI_LLM_API_KEY", "OPENAI_API_KEY", "BANDI_LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    loaded = BandiConfig.load()
    assert loaded.llm.api_key == "test-key", "config 必须能加载 api_key"
    spec = ModelSpec(name="custom-gateway", provider="openai_compat", context_window=32_000)
    assert from_model_spec(spec).available() is True, "config 配了凭证必须可用"


def test_contract_cache_key_includes_provider() -> None:
    """契约 4：ProviderRegistry 缓存键必须含 provider 名（防热切换返回旧实例复发）。"""
    from bandi.llm.mock import MockProvider
    from bandi.llm.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register("mock_a", MockProvider)
    registry.register("mock_b", MockProvider)
    registry.register_model("m1", "mock_a")

    cfg_a = BandiConfig()
    cfg_a.llm.provider = "mock_a"
    cfg_a.llm.base_url = "https://shared.example.com"
    cfg_b = BandiConfig()
    cfg_b.llm.provider = "mock_b"
    cfg_b.llm.base_url = "https://shared.example.com"

    p_a = registry.get_provider("m1", cfg_a)
    p_b = registry.get_provider("m1", cfg_b)
    assert p_a is not p_b, "不同 provider 必须不同实例（缓存键缺 provider 复发）"


def test_contract_budget_self_heals_across_days() -> None:
    """契约 5：预算跨日自愈（防昨天超预算今天永远拦截复发）。"""
    import time

    from bandi.llm.cost_tracker import CostTracker

    tracker = CostTracker(catalog=ModelCatalog(), budget_limit=0.3)
    tracker.record("s1", "deepseek", "deepseek-v4-pro", prompt_tokens=100_000, completion_tokens=0)
    assert tracker.check_budget() is False, "当日超预算拦截"
    tracker._entries[-1].timestamp = time.time() - 86400 * 2
    assert tracker.check_budget() is True, "跨日必须自愈"


def test_contract_max_output_tokens_positive() -> None:
    """契约 6：max_output_tokens 必须正值（防 0/负值每请求显式失败复发）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LLMConfig(max_output_tokens=0)
    with pytest.raises(ValidationError):
        LLMConfig(max_output_tokens=-1)


def test_contract_llm_response_provider_is_real_name() -> None:
    """契约 7：LLM_RESPONSE 事件 provider 字段必须是真实 provider 名（防台账失真复发）。"""
    import asyncio

    from bandi.agent.base import AgentConfig, ExecutionContext
    from bandi.agent.states import on_thinking
    from bandi.core.abort_controller import AbortController
    from bandi.core.config import LLMConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.events import EventType
    from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
    from bandi.llm.base import BaseProvider

    class StubProvider(BaseProvider):
        def __init__(self, config=None):
            super().__init__(config)
            self.call_count = 0

        @property
        def model_name(self) -> str:
            return "stub"

        async def complete(self, messages, tools=None, **kwargs):
            self.call_count += 1
            return LLMResponse(content="ok", usage=TokenUsage(1, 1))

        async def stream(self, messages, tools=None, **kwargs):
            from bandi.llm.streaming import StreamEvent, StreamEventType

            yield StreamEvent(
                type=StreamEventType.DONE, finish_reason="stop", usage=TokenUsage(1, 1)
            )

    class RecordingBus(EventBus):
        def __init__(self):
            super().__init__()
            self.events = []

        async def emit(self, event):
            self.events.append(event)
            await super().emit(event)

    async def _run():
        provider = StubProvider(config=LLMConfig(provider="deepseek"))
        bus = RecordingBus()
        ctx = ExecutionContext(
            messages=[Message(role=MessageRole.USER, content="hi")],
            provider=provider,
            config=AgentConfig(),
            bus=bus,
        )
        ctx.abort_controller = AbortController()
        await on_thinking(ctx)
        resp = [e for e in bus.events if e.type == EventType.LLM_RESPONSE]
        assert resp, "必须产生 LLM_RESPONSE"
        assert resp[0].data["provider"] == "deepseek", (
            f"provider 字段必须是真实名，实际 {resp[0].data.get('provider')!r}"
        )

    asyncio.run(_run())


def test_contract_complexity_pool_filtered_by_availability(monkeypatch) -> None:
    from bandi.llm.capabilities.profile import BackendClass, CapabilityProfile, Modality, TaskType
    from bandi.llm.capabilities.registry import CapabilityRegistry
    from bandi.llm.strategy import CapabilityFallback

    registry = CapabilityRegistry()
    pool = ModelRouter().select_pool(10000, 10)
    # 池中第一个模型注册为不可用（未配凭证）
    unavailable = pool[0]
    offline = CapabilityProfile(
        id=f"chat:test:{unavailable}",
        provider="test",
        backend_class=BackendClass.TEXT_LLM,
        inputs=frozenset({Modality.TEXT}),
        outputs=frozenset({Modality.TEXT}),
        tasks=frozenset({TaskType.CHAT}),
        default_model=unavailable,
        availability=lambda: False,
    )
    registry.register(offline)
    fallback = CapabilityFallback(capability_registry=registry, config=None)

    # preferred 用另一个模型（非复杂度池成员），池中不可用候选必须被滤掉
    preferred = "deepseek-v4-flash"

    async def _candidates() -> list[str]:
        return fallback._candidate_models(preferred, [{"role": "user", "content": "x" * 80000}])

    models = asyncio.run(_candidates())
    assert unavailable not in models, f"不可用模型 {unavailable} 不得进入候选链，实际: {models}"


def test_contract_openai_compat_max_tokens_clamped() -> None:
    import httpx

    from bandi.llm.model_catalog import BUILTIN_MODELS
    from bandi.llm.providers.openai import OpenAICompatProvider

    sent: dict = {}

    async def fake_post(*args, **kwargs):
        payload = kwargs.get("json")
        sent.update(payload if isinstance(payload, dict) else json.loads(payload or "{}"))
        req = httpx.Request("POST", "https://test.example.com/v1")
        resp = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=req)
        return resp

    cfg = LLMConfig(
        provider="openai_compat",
        base_url="https://test.example.com/v1",
        api_key="k",
        max_output_tokens=65536,  # 全局调高（适配 deepseek），超过某模型上限
    )
    provider = OpenAICompatProvider(cfg, cfg.base_url)
    provider._client.post = fake_post
    from bandi.core.config import BandiConfig

    oc = BandiConfig()
    oc.llm = cfg
    spec = BUILTIN_MODELS.get(cfg.default_model)
    expected = (
        min(cfg.max_output_tokens, spec.max_output_tokens)
        if spec is not None
        else cfg.max_output_tokens
    )

    async def _run():
        await provider.complete([{"role": "user", "content": "hi"}])

    asyncio.run(_run())
    assert sent["max_tokens"] == expected, (
        f"max_tokens 必须钳制到 {expected}，实际 {sent.get('max_tokens')}"
    )


def test_contract_google_thinking_level_is_valid_enum() -> None:
    """Google thinking 形状必须合法（非法形状会被 API 400 拒）。

    -C2升级：整数 `thinkingBudget` 已弃用（官方「不再建议」）
    现行字段为 `thinkingConfig.thinkingLevel` 枚举（minimal/low/medium/high）。
    本用例保留原意（**防形状非法 400**），并把「不得再用弃用字段」钉住。
    """
    import httpx

    from bandi.llm.adapters.google import _GEMINI_LEVEL, google_complete

    sent: dict = {}

    async def fake_post(*args, **kwargs):
        payload = kwargs.get("json")
        sent.update(payload if isinstance(payload, dict) else json.loads(payload or "{}"))
        req = httpx.Request("POST", "https://test.example.com/v1")
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}, request=req
        )

    client = httpx.AsyncClient()
    client.post = fake_post

    async def _run():
        await google_complete(
            client,
            api_key="k",
            model="gemini-2.0-flash",
            messages=[{"role": "user", "content": "hi"}],
            thinking_mode="high",
            max_tokens=1024,
        )

    asyncio.run(_run())
    config = sent.get("generationConfig", {}).get("thinkingConfig", {})
    level = config.get("thinkingLevel")
    assert isinstance(level, str), f"thinkingLevel 必须是枚举字符串，实际 {level!r}"
    assert level in set(_GEMINI_LEVEL.values()), (
        f"thinkingLevel 非法取值 {level!r}（会被 API 400 拒）"
    )
    # 破坏者：弃用的整数 budget 字段不得再出现（用户显式选挡位时也不得回退旧字段）
    assert "thinkingBudget" not in json.dumps(config), f"不得再下发弃用字段：{config}"


def test_contract_stream_merge_error_raises() -> None:
    from bandi.core.exceptions import LLMError
    from bandi.llm.streaming import StreamEvent, StreamEventType, merge_stream_events

    async def _gen():
        yield StreamEvent(type=StreamEventType.CONTENT_DELTA, content_delta="partial")
        yield StreamEvent(type=StreamEventType.ERROR, error_message="mid-stream failure")

    async def _run():
        with pytest.raises(LLMError, match="mid-stream failure"):
            await merge_stream_events(_gen())

    asyncio.run(_run())


def test_contract_build_provider_binds_circuit_breaker() -> None:
    """
    重试策略（with_circuit_breaker 生产零调用 = LLM 熔断重试链路形同虚设）。"""
    from bandi.agent.factory import build_provider
    from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy
    from bandi.llm.mock import MockProvider

    cfg = BandiConfig()
    cfg.llm.provider = "openai_compat"
    cfg.llm.api_key = "test-key"
    cfg.llm.base_url = "https://test.example.com/v1"

    provider = build_provider(cfg)
    assert not isinstance(provider, MockProvider), "配了凭证必须真实 provider"
    assert isinstance(provider._circuit_breaker, CircuitBreaker), (
        "provider 必须绑定熔断器（_circuit_breaker 不得为 None）"
    )
    assert isinstance(provider._retry_policy, RetryPolicy), (
        "provider 必须绑定重试策略（_retry_policy 不得为 None）"
    )
