from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import httpx

from bandi.core.config import LLMConfig
from bandi.core.message import LLMResponse
from bandi.llm.base import BaseProvider
from bandi.llm.streaming import StreamEvent, StreamEventType

logger = logging.getLogger(__name__)


def _clamp_tokens(value: int, model: str) -> int:
    """功能：按模型目录上限钳制输出 token 数（catalog 无该模型时按原值透传）。"""
    from bandi.llm.model_catalog import BUILTIN_MODELS

    spec = BUILTIN_MODELS.get(model)
    if spec is None:
        return value
    return min(value, spec.max_output_tokens)


def _clamp_max_tokens(config: LLMConfig) -> int:
    """功能：按 config.max_output_tokens 取预算，并钳制到模型目录上限
    （如调高到 65536 适配 deepseek，超 gemini/anthropic 模型上限时请求 400）。"""
    return _clamp_tokens(config.max_output_tokens, config.default_model)


_OUTPUT_PROMPT_HEADROOM = 256


def _estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """功能：估算本次 prompt token（tiktoken 同源；异常回落 0=跳过动态钳制）。"""
    from bandi.utils.token_counter import TokenCounter

    try:
        total = 0
        for m in messages:
            if isinstance(m, dict):
                total += TokenCounter.count(str(m.get("content") or ""))
                for tc in m.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        total += TokenCounter.count(str(tc.get("arguments") or ""))
            else:
                total += TokenCounter.count(str(getattr(m, "content", "") or ""))
        return total
    except Exception:  # noqa: BLE001 — 估算失败不阻断请求
        return 0


def _clamp_against_prompt(config: LLMConfig, resolved: int, prompt_tokens: int | None) -> int:
    """功能：按本次输入规模动态钳制输出预算——
    budget = 目录窗口 - prompt - 余量；目录未收录/未传 prompt 时按原值透传。
    静态目录钳制无法感知本次输入规模，超长 prompt + 大输出预算叠加会超窗 400。"""
    if not prompt_tokens or prompt_tokens <= 0:
        return resolved
    from bandi.llm.model_catalog import BUILTIN_MODELS

    spec = BUILTIN_MODELS.get(config.default_model)
    window = int(getattr(spec, "context_window", 0) or 0) if spec is not None else 0
    if window <= 0:
        return resolved
    budget = window - prompt_tokens - _OUTPUT_PROMPT_HEADROOM
    return max(1, min(resolved, budget))


def _resolve_max_tokens(
    config: LLMConfig, override: Any = None, *, prompt_tokens: int | None = None
) -> int:
    """功能：解析本次请求的输出预算——调用方显式覆盖优先于 config（非法/非正值回落
    config），保证 accounted_complete/蒸馏器等旁路传入的 max_tokens 不被吞掉；
    并叠加目录上限钳制 + 输入规模动态钳制（prompt_tokens 传入时）。"""
    if override is not None:
        try:
            value = int(override)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            resolved = _clamp_tokens(value, config.default_model)
            return _clamp_against_prompt(config, resolved, prompt_tokens)
    return _clamp_against_prompt(config, _clamp_max_tokens(config), prompt_tokens)


class HttpAdapterProvider(
    BaseProvider
):  # 功能：httpx 客户端 + adapter 注入的 HTTP Provider 共享基类
    _adapter_complete: Callable[..., Awaitable[LLMResponse]] | None = None
    _adapter_stream: Callable[..., AsyncGenerator[StreamEvent, None]] | None = None
    _provider_label: str = ""
    _protocol: str = "openai"
    _api_protocol: str = "openai_compat"

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self._timeout = getattr(config, "timeout", 60)
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        # 透传 config.max_output_tokens；并按 ModelSpec.max_output_tokens 钳制
        return await self._call_with_retry(
            self._adapter_complete,
            client=self._client,
            api_key=self.auth_token,
            model=self.config.default_model,
            messages=messages,
            tools=tools,
            timeout=self._timeout,
            max_tokens=_resolve_max_tokens(
                self.config,
                kwargs.get("max_tokens"),
                prompt_tokens=_estimate_prompt_tokens(messages),
            ),
            thinking_mode=self.config.thinking_mode,
            base_url=self.config.base_url,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncGenerator[StreamEvent, None]:
        emitted = False
        try:
            async for event in self._adapter_stream(
                self._client,
                self.auth_token,
                self.config.default_model,
                messages,
                tools=tools,
                timeout=self._timeout,
                max_tokens=_resolve_max_tokens(
                    self.config,
                    kwargs.get("max_tokens"),
                    prompt_tokens=_estimate_prompt_tokens(messages),
                ),
                thinking_mode=self.config.thinking_mode,
                base_url=self.config.base_url,
            ):
                emitted = True
                yield event
        except Exception as e:
            logger.debug("stream failed: %s", e)
            if not emitted:
                # 首事件前失败：保留原始异常给候选/非流式回退链判断（超长/鉴权/网络）。
                raise
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error_message=f"模型流式请求失败（{self._provider_label}），请重试或切换模型。",
            )

    @property
    def protocol(self) -> str:
        return self._protocol

    @property
    def api_protocol(self) -> str:
        return self._api_protocol
