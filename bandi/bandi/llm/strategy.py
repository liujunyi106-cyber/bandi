# 功能：Provider 回退策略（FallbackEngine 链式降级 + 能力/模型回退 + 复杂度预估）
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import httpx

from bandi.core.message import LLMResponse
from bandi.llm.streaming import StreamEvent

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_MS_PER_SEC = 1000

_PROMPT_TOO_LONG_MARKERS = (
    "context length",
    "prompt is too long",
    "too many tokens",
    "maximum context",
    "input length",
)

_PROMPT_TOO_LONG_413_RE = re.compile(r"\b413\b")


def is_prompt_too_long_error(exception: BaseException) -> bool:
    """功能：识别各 provider 形态的超长错误（文本特征匹配，覆盖 httpx 400/413 与 措辞）。

    httpx.HTTPStatusError 的 str() 只含状态行不含响应体——超长关键词通常在
    response.text 中，故一并检查（E2E 实证缺陷修复）。
    """
    texts = [str(exception)]
    response_text = getattr(getattr(exception, "response", None), "text", None)
    if response_text:
        texts.append(response_text)
    for text in texts:
        lowered = text.lower()
        if any(marker in lowered for marker in _PROMPT_TOO_LONG_MARKERS):
            return True
        if _PROMPT_TOO_LONG_413_RE.search(lowered):
            return True
    return False


@dataclass
class RecoveryAction:
    action: Literal["retry", "fallback", "backoff", "compact", "replan"]
    max_retries: int = 2
    base_delay: float = 1.0
    compact_before_retry: bool = False


RECOVERY_STRATEGIES: dict[str, RecoveryAction] = {
    "LLM_RATE_LIMIT": RecoveryAction("backoff", max_retries=3, base_delay=1.0),
    "LLM_TIMEOUT": RecoveryAction("fallback", max_retries=1),
    "LLM_SERVICE_ERROR": RecoveryAction("fallback", max_retries=2),
    "LLM_PROMPT_TOO_LONG": RecoveryAction("compact", compact_before_retry=True),
    "TOOL_EXEC_FAILED": RecoveryAction("replan"),
}


class FallbackEngine:
    def __init__(
        self,
        fallback_chain: list[str] | None = None,
    ) -> None:
        self._chain = fallback_chain or []

    async def call_with_fallback(
        self,
        messages: list[dict],
        config: Any,
        original_call: Callable[..., Awaitable[_T]],
    ) -> _T:
        providers = self._chain if self._chain else [config.get("provider", "default")]
        last_error: Exception | None = None
        for provider_name in providers:
            try:
                cfg = dict(config)
                cfg["provider"] = provider_name
                return await original_call(messages, cfg)
            except Exception as e:
                last_error = e
                action = self._classify(e)
                if action.action == "compact":
                    raise
                if action.action == "backoff":
                    # 逐次退避重试同一 provider（失败继续退避，上限 max_retries）
                    for attempt in range(action.max_retries):
                        await asyncio.sleep(action.base_delay * (2**attempt))
                        try:
                            return await original_call(messages, cfg)
                        except Exception as e2:  # noqa: BLE001
                            last_error = e2
                            if self._classify(e2).action != "backoff":
                                break
                    continue
                logger.warning("fallback %s -> %s", provider_name, str(e))
                continue
        raise ProviderChainExhaustedError(last_error)

    def _classify(self, exception: Exception) -> RecoveryAction:
        # 超长优先于类型映射：compact 动作由 states 层统一接管（重试压缩），此处不外切 provider
        if is_prompt_too_long_error(exception):
            return RECOVERY_STRATEGIES["LLM_PROMPT_TOO_LONG"]
        # 真实异常族（httpx——生产 provider 的实际抛出类型）
        if isinstance(exception, httpx.HTTPStatusError):
            status = exception.response.status_code
            if status == 429:
                return RECOVERY_STRATEGIES["LLM_RATE_LIMIT"]
            if status >= 500:
                return RECOVERY_STRATEGIES["LLM_SERVICE_ERROR"]
            return RecoveryAction("fallback")  # 4xx（凭证/参数）不可恢复：切候选
        if isinstance(exception, httpx.TimeoutException):
            return RECOVERY_STRATEGIES["LLM_TIMEOUT"]
        if isinstance(exception, httpx.TransportError):
            return RECOVERY_STRATEGIES["LLM_SERVICE_ERROR"]
        mappings: dict[str, str] = {
            "RateLimitError": "LLM_RATE_LIMIT",
            "TimeoutError": "LLM_TIMEOUT",
            "asyncio_timeout": "LLM_TIMEOUT",
            "APIStatusError": "LLM_SERVICE_ERROR",
            "APIConnectionError": "LLM_SERVICE_ERROR",
        }
        key = mappings.get(type(exception).__name__, "")
        if key:
            return RECOVERY_STRATEGIES[key]
        if isinstance(exception, (asyncio.TimeoutError, TimeoutError)):
            return RECOVERY_STRATEGIES["LLM_TIMEOUT"]
        # 未知异常：切候选（不落未实现的 retry 分支名）
        return RecoveryAction("fallback")


class ProviderChainExhaustedError(Exception): ...


class CapabilityFallback:  # 功能：能力感知的多任务 fallback（候选=钉选 preferred → CapabilityRouter 排序 → 失败切换 + stats 反馈）
    def __init__(self, capability_registry: Any, config: Any, stats: Any = None) -> None:
        from bandi.llm.capabilities.provider_stats import get_provider_stats
        from bandi.llm.capabilities.router import CapabilityRouter

        self._registry = capability_registry
        self._config = config
        self._stats = stats or get_provider_stats()
        self._router = CapabilityRouter(capability_registry, stats=self._stats)

    def _candidate_models(self, preferred_model: str, messages: list[dict]) -> list[str]:
        # 不做模型候选链：配置/应用什么模型就用什么——单候选=钉选 preferred
        return [preferred_model]

    def _provider_name(self, model: str) -> str:
        for profile in self._registry.all():
            if profile.default_model == model:
                return profile.provider
        return model

    def ordered_candidates(self, preferred_model: str, messages: list[dict]) -> list[str]:
        # 功能：暴露候选序列（钉选 preferred 优先）供流式降级分支与非流式共用同源排序
        return self._candidate_models(preferred_model, messages)

    async def complete_with_fallback(
        self,
        preferred_model: str,
        messages: list[dict],
        registry: Any,
        config: Any,
        **kwargs: Any,
    ) -> tuple[LLMResponse, str]:

        models = self._candidate_models(preferred_model, messages)
        last_error: Exception | None = None
        for model in models:
            provider_name = self._provider_name(model)
            try:
                provider = registry.get_provider(model, config)
                start = time.perf_counter()
                result = await asyncio.wait_for(
                    provider.complete(messages, **kwargs),
                    timeout=config.llm.timeout if config.llm else 60,
                )
                latency_ms = (time.perf_counter() - start) * _MS_PER_SEC
                self._stats.record_attempt(
                    "chat", provider_name, success=True, latency_ms=latency_ms
                )
                return result, model
            except Exception as e:
                # 同样超长，继续切换纯属浪费网络调用），由上层响应式压缩处理
                if is_prompt_too_long_error(e):
                    raise
                logger.debug("chat attempt failed: %s", e)
                self._stats.record_attempt("chat", provider_name, success=False)
                last_error = e
                continue

        raise ProviderChainExhaustedError(
            f"All capability candidates failed: {models}, last error: {last_error}"
        )


class ModelFallback:
    def __init__(
        self,
        fallback_chain: list[str] | None = None,
    ) -> None:
        self._chain = fallback_chain or []

    def get_fallback_chain(self) -> list[str]:
        return list(self._chain)

    async def complete_with_fallback(
        self,
        preferred_model: str,
        messages: list[dict],
        registry: Any,
        config: Any,
        **kwargs: Any,
    ) -> tuple[LLMResponse, str]:
        from bandi.core.exceptions import LLMError

        models = [preferred_model]
        for m in self._chain:
            if m != preferred_model:
                models.append(m)

        last_error: Exception | None = None
        for model in models:
            try:
                provider = registry.get_provider(model, config)
                result = await asyncio.wait_for(
                    provider.complete(messages, **kwargs),
                    timeout=config.llm.timeout if config.llm else 60,
                )
                return result, model
            except (LLMError, RuntimeError, TimeoutError) as e:
                last_error = e
                continue

        raise ProviderChainExhaustedError(
            f"All models in fallback chain failed: {models}, last error: {last_error}"
        )


_SIMPLE_TOKEN_THRESHOLD = 1000
_SIMPLE_TOOL_THRESHOLD = 3
_MODERATE_TOKEN_THRESHOLD = 4000
_MODERATE_TOOL_THRESHOLD = 6


class ComplexityEstimator:
    def estimate(self, input_tokens: int, tool_count: int = 0) -> str:
        if input_tokens < _SIMPLE_TOKEN_THRESHOLD and tool_count < _SIMPLE_TOOL_THRESHOLD:
            return "simple"
        if input_tokens < _MODERATE_TOKEN_THRESHOLD and tool_count < _MODERATE_TOOL_THRESHOLD:
            return "moderate"
        return "complex"


# 候选池全部模型必须在 BUILTIN_MODELS 注册——未注册模型在候选池中会导致
# 每轮 fallback 必产生无效尝试。
# 首选 .1-Flash，且**双命名并列**：`deepseek-v4.1-flash`（网关命名，OpenRouter 等
# 20+ 家）与 `deepseek-flash`（api.deepseek.com 官方现役名）指向同一模型——端点若拒绝
# 其中一个名字，池中另一项仍在同一轮内兜住，避免整个 simple/moderate 档全灭。
# 比现役价（1.08/4.32）高约 47% ⇒ 已移出池（会让成本与预算记账偏高）。
_SIMPLE_POOL: list[str] = [
    "deepseek-v4.1-flash",
    "deepseek-flash",
    "glm-5.3-flash",
    "gpt-5-nano",
]
_MODERATE_POOL: list[str] = [
    "deepseek-v4.1-flash",
    "deepseek-flash",
    "glm-5.2",
    "kimi-k2.7-code",
    "gpt-5-mini",
]
_COMPLEX_POOL: list[str] = ["gpt-5.2", "deepseek-v4-pro", "kimi-k3", "glm-5.3"]


class ModelRouter:
    def __init__(self, estimator: ComplexityEstimator | None = None) -> None:
        self._estimator = estimator or ComplexityEstimator()

    def select_pool(self, input_tokens: int, tool_count: int = 0) -> list[str]:
        level = self._estimator.estimate(input_tokens, tool_count)
        if level == "simple":
            return list(_SIMPLE_POOL)
        if level == "moderate":
            return list(_MODERATE_POOL)
        return list(_COMPLEX_POOL)

    def select_best(self, input_tokens: int, tool_count: int = 0) -> str:
        pool = self.select_pool(input_tokens, tool_count)
        return pool[0]

    async def stream_with_fallback(
        self,
        preferred_model: str,
        # states 层 day124x-q CapabilityFallback + _stream_thinking 承担；本方法
        # 保留仅作兼容导出，勿再接线重复实现
        messages: list[dict],
        registry: Any,
        config: Any,
        **kwargs: Any,
    ) -> tuple[AsyncGenerator[StreamEvent, None], str]:
        """返回 (流, 首选模型)。流内部惰性降级：首选模型迭代期失败自动切换到候选池。

        注：async generator 惰性执行，实际使用的模型只能在迭代期间确定；
        返回的 str 恒为首选模型，降级语义由调用方通过观察流内容自行判断。
        """
        from bandi.core.exceptions import LLMError

        input_tokens = sum(len(str(m)) // 4 for m in messages)
        pool = self.select_pool(input_tokens)
        models = [preferred_model] + [m for m in pool if m != preferred_model]

        async def _fallback_stream() -> AsyncGenerator[StreamEvent, None]:
            last_error: Exception | None = None
            for model in models:
                try:
                    provider = registry.get_provider(model, config)
                    async for event in provider.stream(messages, **kwargs):
                        yield event
                    return
                except (LLMError, RuntimeError) as e:
                    last_error = e
                    continue
            raise ProviderChainExhaustedError(
                f"All models in fallback chain failed: {models}, last error: {last_error}"
            )

        return _fallback_stream(), preferred_model
