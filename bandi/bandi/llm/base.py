# 功能：LLM Provider 抽象基类（complete/stream + 熔断重试支持 + 统一鉴权）
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Self

from bandi.core.config import LLMConfig
from bandi.core.message import LLMResponse
from bandi.llm.circuit_breaker import CircuitBreaker, RetryPolicy
from bandi.llm.streaming import StreamEvent, StreamEventType  # noqa: F401


class BaseProvider(ABC):  # 功能：LLM 提供方抽象基类（complete/stream + 熔断/重试支持）
    def __init__(self, config: LLMConfig):
        self.config = config
        self._circuit_breaker = None
        self._retry_policy = None
        self._auth_token: str = getattr(config, "api_key", "") or ""

    @abstractmethod
    async def complete(  # 功能：发送非流式补全请求，返回完整 LLMResponse（messages 已由 message_to_dict 转为 dict）
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse: ...

    @abstractmethod
    async def stream(  # 功能：发送流式补全请求，通过 AsyncGenerator 产出 StreamEvent（messages 已由 message_to_dict 转为 dict）
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    def count_tokens(self, text: str) -> int:
        from bandi.utils.token_counter import TokenCounter

        return TokenCounter.count(text)

    async def _call_with_retry(self, fn, **kwargs):  # 功能：在重试策略+熔断器保护下调用函数
        if self._retry_policy is not None:
            return await self._retry_policy.execute(
                fn, circuit_breaker=self._circuit_breaker, **kwargs
            )
        return await fn(**kwargs)

    async def close(self) -> None:  # 功能：释放 Provider 持有的底层资源（子类按需重写）
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def with_circuit_breaker(
        self, cb: CircuitBreaker, rp: RetryPolicy
    ) -> BaseProvider:  # 功能：绑定熔断器和重试策略
        self._circuit_breaker = cb
        self._retry_policy = rp
        return self

    @property
    def protocol(
        self,
    ) -> (
        str
    ):  # 功能：获取消息序列化协议名（openai/anthropic/google），用于 message_to_dict 选择格式
        return "openai"

    @property
    def api_protocol(
        self,
    ) -> str:  # 功能：获取 API 兼容协议名（openai_compat/anthropic/google），用于 Adapter 路由
        return "openai_compat"

    @property
    def model_name(self) -> str:
        return self.config.default_model

    @property
    def supports_streaming(self) -> bool:
        return True

    @property
    def auth_token(self) -> str:
        """功能：获取鉴权 token（优先返回显式设置的 token，回退到 config.api_key）"""
        return self._auth_token

    @auth_token.setter
    def auth_token(self, value: str) -> None:
        """功能：设置鉴权 token（支持 OAuth / API Key / Bearer）"""
        self._auth_token = value if value else ""

    def set_auth_token(self, token: str) -> None:
        """功能：显式设置鉴权 token（别名方法）"""
        self.auth_token = token
