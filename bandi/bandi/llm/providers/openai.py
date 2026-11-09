from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any, Self

import httpx

from bandi.core.config import LLMConfig
from bandi.core.exceptions import llm_error_with_status
from bandi.core.message import LLMResponse
from bandi.llm.adapters.openai_compat import openai_complete, sse_stream
from bandi.llm.base import BaseProvider
from bandi.llm.providers._http_provider import _estimate_prompt_tokens, _resolve_max_tokens
from bandi.llm.streaming import StreamEvent, StreamEventType

logger = logging.getLogger(__name__)


class OpenAICompatProvider(
    BaseProvider
):  # 功能：OpenAI 兼容协议基类（OpenAI/DeepSeek/GLM/Qwen 等公用）——
    # 请求参数默认不发送厂商私有字段（未知参数 400 风险）；子类按官方语义覆盖
    def __init__(self, config: LLMConfig, base_url: str = ""):
        super().__init__(config)
        # base_url 缺省回退 config.base_url（注册表按名实例化 compat 家族时不传参）
        self._base_url = base_url or getattr(config, "base_url", "") or ""
        self._timeout = getattr(config, "timeout", 60)
        self._client = httpx.AsyncClient(timeout=self._timeout)

    def _thinking_payload(self) -> dict[str, Any]:
        # 功能：thinking 参数翻译钩子——通用兼容端点不发送自定义字段
        return {}

    def _extra_headers(self) -> dict[str, str]:
        # 功能：附加请求头翻译钩子——config.extra_headers 透传（空 = 不发任何自定义头）
        headers = getattr(self.config, "extra_headers", None) or {}
        return {str(k): str(v) for k, v in headers.items()}

    def _max_tokens_field(self) -> str:
        # 功能：输出上限字段名钩子——通用兼容端点用 max_tokens
        return "max_tokens"

    async def close(self) -> None:  # 功能：关闭底层 AsyncClient 释放资源
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def complete(  # 功能：调用 OpenAI 兼容的 /chat/completions API（重试策略保护）
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        # 已有钳制，OpenAICompat 主路线漏，超模型上限请求 400）
        return await self._call_with_retry(
            openai_complete,
            client=self._client,
            base_url=self._base_url,
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
            max_tokens_field=self._max_tokens_field(),
            thinking_payload=self._thinking_payload(),
            extra_headers=self._extra_headers(),
        )

    async def stream(  # 功能：调用 OpenAI 兼容的流式补全 API，SSE 实时产出事件（重试策略保护）
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncGenerator[StreamEvent, None]:
        url = f"{self._base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.auth_token}", "Content-Type": "application/json"}
        headers.update(self._extra_headers())
        body: dict = {"model": self.config.default_model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        body[self._max_tokens_field()] = _resolve_max_tokens(
            self.config,
            kwargs.get("max_tokens"),
            prompt_tokens=_estimate_prompt_tokens(messages),
        )
        body["stream_options"] = {"include_usage": True}
        # 流式请求通过 _call_with_retry 获得基础异常保护（失败不重试完整的 stream，但支持 CircuitBreaker 状态管理）
        # 契约：首事件前失败 raise LLMError（strategy.stream_with_fallback 降级）
        # 首事件后中途失败 yield ERROR（agent/states.py 消费终态）
        emitted = False
        try:
            async for event in sse_stream(
                self._client,
                url,
                headers,
                body,
                thinking_payload=self._thinking_payload(),
            ):
                emitted = True
                yield event
        except Exception as e:
            logger.debug("stream connection failed: %s", e)
            if not emitted:
                raise llm_error_with_status(
                    f"Stream connection failed before first event: {e}", e
                ) from e
            yield StreamEvent(
                type=StreamEventType.ERROR,
                error_message="流式传输中断（回复可能不完整），请重试或继续对话。",
            )


class OpenAIProvider(OpenAICompatProvider):  # 功能：OpenAI API 提供方
    def __init__(self, config: LLMConfig):
        # 显式 config.base_url（代理/中转）优先于官方默认域名
        super().__init__(config, getattr(config, "base_url", "") or "https://api.openai.com/v1")

    _REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")
    # none/low/medium/high/xhigh；max 与 xhigh 同档但保留 xhigh 原名直传）
    _EFFORT_MAP = {
        "none": "none",
        "minimal": "low",
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "xhigh",
        "max": "xhigh",
    }

    def _thinking_payload(self) -> dict[str, Any]:
        # 功能：thinking_mode → OpenAI 官方 reasoning_effort。
        # 空档（UI「关闭」）对推理模型显式发 none——OpenAI 服务端默认 medium（思考开）
        # 不发送等于「默认思考」，与用户选择"关闭"语义相悖；非推理模型不发（防 400）。
        mode = (self.config.thinking_mode or "").strip().lower()
        if not mode:
            model = (self.config.default_model or "").lower()
            if model.startswith(self._REASONING_MODEL_PREFIXES):
                return {"reasoning_effort": "none"}
            return {}
        effort = self._EFFORT_MAP.get(mode)
        return {"reasoning_effort": effort} if effort else {}

    def _max_tokens_field(self) -> str:
        model = (self.config.default_model or "").lower()
        if model.startswith(self._REASONING_MODEL_PREFIXES):
            return "max_completion_tokens"
        return "max_tokens"
