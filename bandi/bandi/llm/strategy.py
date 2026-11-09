# 功能：Provider 回退策略（FallbackEngine 链式降级 + 能力回退）
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import httpx

from bandi.core.message import LLMResponse

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
        candidates: list[str] | None = None,
    ) -> None:
        self._chain = candidates or []

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
        raise ProviderChainExhaustedError(str(last_error), last_error=last_error)

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


class ProviderChainExhaustedError(Exception):
    """功能：候选链耗尽——保留内层异常，供上游沿链提取 HTTP 状态码。"""

    def __init__(self, message: str, last_error: Exception | None = None) -> None:
        super().__init__(message)
        self.last_error = last_error


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
            f"All capability candidates failed: {models}, last error: {last_error}",
            last_error=last_error,
        )
