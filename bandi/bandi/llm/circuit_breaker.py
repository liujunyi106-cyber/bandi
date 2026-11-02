# 功能：熔断器与重试策略（CircuitBreaker/RetryPolicy）
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TypeVar

import httpx

from bandi.utils.backoff import compute_backoff_delay

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# 功能：4xx 中仍值得重试的状态——限流与请求超时是「等一会就好」，不是请求本身错。
_RETRYABLE_STATUS = frozenset({408, 409, 429})


class CircuitState(Enum):  # 功能：熔断器状态枚举（CLOSED/OPEN/HALF_OPEN）
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):  # 功能：熔断器异常基类
    """Base circuit breaker error."""


class CircuitBreakerOpenError(CircuitBreakerError):  # 功能：熔断器断开时拒绝调用
    """Call rejected because circuit is open."""


class CircuitBreaker:  # 功能：熔断器（状态机：关闭→开路→半开探测，防级联故障）
    def __init__(
        self,
        config: dict | None = None,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
        half_open_max_retries: int | None = None,
    ):
        self.state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        cfg = config or {}
        self._failure_threshold = failure_threshold or cfg.get("failure_threshold", 5)
        self._recovery_timeout = recovery_timeout or cfg.get("recovery_timeout", 30.0)
        self._half_open_max_retries = half_open_max_retries or cfg.get("half_open_max_retries", 2)
        self._half_open_attempts = 0

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    async def call(
        self, fn: Callable[..., Awaitable[_T]], *args, **kwargs
    ) -> _T:  # 功能：在熔断器保护下调用函数，半开时允许探测请求
        if self.state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time > self._recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self._half_open_attempts = 0
                logger.info("Circuit breaker transitioning OPEN -> HALF_OPEN")
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit open, call rejected after {self._failure_count} failures"
                )

        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure(type(e).__name__)
            raise

    def _on_success(self) -> None:
        self._failure_count = 0
        self._half_open_attempts = 0
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            logger.info("Circuit breaker HALF_OPEN probe succeeded -> CLOSED")

    def _on_failure(self, error_type: str) -> None:
        self._last_failure_time = time.monotonic()
        if self.state == CircuitState.HALF_OPEN:
            self._half_open_attempts += 1
            if self._half_open_attempts >= self._half_open_max_retries:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker HALF_OPEN probe failed ({error_type}) -> OPEN")
        else:
            self._failure_count += 1
            if self._failure_count >= self._failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(
                    f"Circuit breaker CLOSED -> OPEN after {self._failure_count} failures ({error_type})"
                )

    def reset(self) -> None:  # 功能：手动重置熔断器为 CLOSED 状态
        self.state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_attempts = 0


class RetryPolicy:  # 功能：指数退避重试策略（最大重试/基础延迟/抖动），只重试可恢复异常
    def __init__(
        self,
        config: dict | None = None,
        max_retries: int | None = None,
        base_delay: float | None = None,
        max_delay: float | None = None,
        backoff_factor: float | None = None,
        jitter: bool = True,
    ):
        cfg = config or {}
        self._max_retries = max_retries if max_retries is not None else cfg.get("max_retries", 3)
        self._base_delay = base_delay if base_delay is not None else cfg.get("base_delay", 1.0)
        self._max_delay = max_delay if max_delay is not None else cfg.get("max_delay", 30.0)
        self._backoff_factor = (
            backoff_factor if backoff_factor is not None else cfg.get("backoff_factor", 2.0)
        )
        self._jitter = jitter

    async def execute(  # 功能：执行带重试逻辑的函数调用，支持熔断器保护和指数退避
        self,
        fn: Callable[..., Awaitable[_T]],
        circuit_breaker: CircuitBreaker | None = None,
        **kwargs,
    ) -> _T:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                if circuit_breaker:
                    return await circuit_breaker.call(fn, **kwargs)
                return await fn(**kwargs)
            except CircuitBreakerOpenError:
                raise
            except httpx.HTTPStatusError as e:
                # 4xx 多为请求本身有问题（401/403/404），重试无意义；但 429 限流与
                # 408 请求超时是「等一会就好」，把它们一并判死会让一次正常请求因
                # 瞬时限流失败——L1 verifier judge 撞 429 后 fail-closed
                # 把一条本来正确的记忆召回结果判成 ESCALATED。
                status = e.response.status_code
                if 400 <= status < 500 and status not in _RETRYABLE_STATUS:
                    raise
                last_error = e
                if attempt < self._max_retries:
                    delay = self._compute_delay(attempt)
                    logger.info(
                        f"Retry {attempt + 1}/{self._max_retries} after {delay:.2f}s "
                        f"({type(e).__name__}, {status})"
                    )
                    await asyncio.sleep(delay)
            except (TimeoutError, ConnectionError, OSError) as e:
                last_error = e
                if attempt < self._max_retries:
                    delay = self._compute_delay(attempt)
                    logger.info(
                        f"Retry {attempt + 1}/{self._max_retries} after {delay:.2f}s "
                        f"({type(e).__name__})"
                    )
                    await asyncio.sleep(delay)
            except httpx.HTTPError as e:
                last_error = e
                if attempt < self._max_retries:
                    delay = self._compute_delay(attempt)
                    logger.info(
                        f"Retry {attempt + 1}/{self._max_retries} after {delay:.2f}s "
                        f"(httpx {type(e).__name__})"
                    )
                    await asyncio.sleep(delay)
            except Exception:
                raise
        raise last_error or CircuitBreakerError("retry exhausted")  # type: ignore[misc]

    def _compute_delay(self, attempt: int) -> float:
        # 委托公共退避函数（jitter 因子 0.5~1.0、cap max_delay）——行为与历史实现一致
        return compute_backoff_delay(
            self._base_delay,
            self._backoff_factor,
            attempt,
            max_delay=self._max_delay,
            jitter=self._jitter,
        )
