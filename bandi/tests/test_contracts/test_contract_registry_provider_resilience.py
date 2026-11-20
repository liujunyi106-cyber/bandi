"""功能：注册表构造的 provider 必须挂上重试策略与熔断器。

真机缺陷：重试策略只在 `agent/factory.py` 的「直连 provider」分支装配，
而生产主路径是 `CapabilityFallback` → `registry.get_provider()`。
于是 `_retry_policy` 恒为 None，`_call_with_retry` 退化成裸调：

    # llm/base.py
    if self._retry_policy is not None:
        return await self._retry_policy.execute(...)
    return await fn(**kwargs)          # ← 生产走这里，零重试

后果：网关一次瞬断（anyio `All connection attempts failed`）即
`ProviderChainExhaustedError`，用户看到内部异常串，且不弹任何重试提示。

本契约钉两件事：
1. `registry.get_provider()` 返回的实例 `_retry_policy` / `_circuit_breaker` 非空；
2. 重复获取（缓存命中路径）保持已装配且幂等。
"""

from __future__ import annotations

from bandi.core.config import BandiConfig
from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy
from bandi.llm.registry import ProviderRegistry

# 功能：契约自带网关注解——不读用户档案，避免测试依赖 ~/.bandi/llm_profiles.json
_CUSTOM_MODEL = "contract-stub-model"
_CUSTOM_BASE_URL = "https://contract-stub.invalid/v1"


def _config() -> BandiConfig:
    """功能：构造带显式 base_url 的配置（自定义网关语义）。"""
    cfg = BandiConfig.load()
    assert cfg.llm is not None
    cfg.llm.default_model = _CUSTOM_MODEL
    cfg.llm.base_url = _CUSTOM_BASE_URL
    cfg.llm.api_key = "contract-stub-key"
    return cfg


def _provider() -> object:
    cfg = _config()
    # resolve_profile=False：不套用用户档案覆盖，直接走自定义网关分支
    return ProviderRegistry().get_provider(_CUSTOM_MODEL, cfg, resolve_profile=False)


def test_registry_provider_has_retry_policy() -> None:
    """功能：注册表路径的 provider 必须能重试（否则瞬断即整轮失败）。"""
    provider = _provider()

    assert isinstance(getattr(provider, "_retry_policy", None), RetryPolicy), (
        "registry 构造的 provider 缺 _retry_policy："
        "生产主路径会绕过所有重试，一次瞬断就抛 ProviderChainExhaustedError"
    )
    assert isinstance(getattr(provider, "_circuit_breaker", None), CircuitBreaker)


def test_registry_provider_retry_budget_is_effective() -> None:
    """功能：重试预算必须 >0，否则策略等于没装。"""
    policy = getattr(_provider(), "_retry_policy", None)

    assert policy is not None
    assert policy._max_retries >= 1, "max_retries <1 等于不重试"
    assert policy._base_delay > 0, "必须退避，否则瞬断期间疯狂打同一端点"


def test_cached_instance_keeps_policy_and_is_idempotent() -> None:
    """功能：二次获取（缓存命中）仍已装配；重复 attach 不得叠加。"""
    registry = ProviderRegistry()
    cfg = _config()
    first = registry.get_provider(_CUSTOM_MODEL, cfg, resolve_profile=False)
    policy_first = getattr(first, "_retry_policy", None)

    second = registry.get_provider(_CUSTOM_MODEL, cfg, resolve_profile=False)
    assert second is first, "同参数应复用同一实例"
    assert getattr(second, "_retry_policy", None) is policy_first, "重复 attach 不得替换策略"

    # 再 attach 一次（模拟并发/重复装配）不应改变已有策略
    registry._attach_resilience(second)
    assert getattr(second, "_retry_policy", None) is policy_first
