# 功能：降级恢复管理（错误分级 + 降级状态机 + 恢复策略）
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from bandi.agent.base import RecoveryAction
from bandi.core.events import Event, EventType

if TYPE_CHECKING:
    from bandi.core.event_bus import EventBus

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    FATAL = auto()
    RECOVERABLE = auto()
    DEGRADED = auto()


@dataclass
class DegradationState:
    degraded_capabilities: dict[str, str] = field(default_factory=dict)

    def degrade(self, layer: str, reason: str) -> None:
        self.degraded_capabilities[layer] = reason

    def recover(self, layer: str) -> None:
        self.degraded_capabilities.pop(layer, None)

    def is_degraded(self, layer: str) -> bool:
        return layer in self.degraded_capabilities

    @property
    def any_degraded(self) -> bool:
        return len(self.degraded_capabilities) > 0

    def snapshot(self) -> dict[str, str]:
        return dict(self.degraded_capabilities)


class RecoveryManager:
    def __init__(self, bus: EventBus | None = None, escalate_threshold: int = 3):
        # escalate_threshold：同一层连续错误达此数即升级终止。默认 3（对齐参考源
        # LeAgent `_TRANSIENT_RETRY_ATTEMPTS=3` / deepseek-harness `maxRetries=2`）——
        # 10 次会让上游故障场景白等数分钟（真机实证：503 重试 10 次 ≈ 2.5 分钟）。
        self.consecutive_errors: dict[str, int] = {}
        self.degradation = DegradationState()
        self._bus = bus
        self._escalate_threshold = max(1, int(escalate_threshold))
        self._lock = threading.Lock()

    def _classify(self, error: Exception, layer: str) -> ErrorSeverity:
        # 功能：错误分档——决定「重试整个回合 / 降级继续 / 立即终止」
        #
        # 无可用候选分两类（参考源共识：deepseek-harness `INVALID_CREDENTIAL` 排除默认重试集，
        # 理由是「每次尝试都会同样失败」；opencode `server_error`/`overloaded` 判可重试）：
        # - 配置类（模型名不在目录 / 无 base_url / 无 provider / 4xx 非 429）→ FATAL 立即终止；
        # - 暂时故障（429 限流 / 5xx 上游故障）→ RECOVERABLE，重试有意义。
        from bandi.core.exceptions import (
            LLMUnavailableError,
            is_transient_upstream,
            upstream_status,
        )

        if isinstance(error, LLMUnavailableError):
            return (
                ErrorSeverity.RECOVERABLE if is_transient_upstream(error) else ErrorSeverity.FATAL
            )
        if isinstance(error, (TypeError, KeyError, AttributeError)):
            return ErrorSeverity.FATAL
        status = upstream_status(error)
        if status is not None and 400 <= status < 500 and status != 429:
            # 客户端/配置类错误（400/401/403/404/413/422…）——重试同一请求必然同样失败。
            # 429 属限流，走下方默认 RECOVERABLE（与 5xx 同档）。
            return ErrorSeverity.FATAL
        if isinstance(error, ValueError):
            return ErrorSeverity.RECOVERABLE
        if isinstance(error, (TimeoutError, ConnectionError, BrokenPipeError)):
            return ErrorSeverity.RECOVERABLE
        if isinstance(error, NotImplementedError):
            return ErrorSeverity.DEGRADED
        return ErrorSeverity.RECOVERABLE

    async def handle(self, error: Exception, layer: str) -> RecoveryAction:
        severity = self._classify(error, layer)
        with self._lock:
            if severity == ErrorSeverity.RECOVERABLE:
                self.consecutive_errors[layer] = self.consecutive_errors.get(layer, 0) + 1
            elif severity == ErrorSeverity.FATAL:
                self.consecutive_errors.pop(layer, None)

            if self.consecutive_errors.get(layer, 0) >= self._escalate_threshold:
                return RecoveryAction.ESCALATE

        match severity:
            case ErrorSeverity.FATAL:
                return RecoveryAction.TERMINATE
            case ErrorSeverity.RECOVERABLE:
                return RecoveryAction.RETRY
            case ErrorSeverity.DEGRADED:
                self.degradation.degrade(layer, str(error))
                await self._emit_degradation(layer, str(error))
                return RecoveryAction.CONTINUE_DEGRADED

    def on_turn_completed(self) -> None:
        # 少量错误永远触不到阈值）；每轮完成每层减 1，持续退化仍可累积
        with self._lock:
            for layer in list(self.consecutive_errors):
                self.consecutive_errors[layer] -= 1
                if self.consecutive_errors[layer] <= 0:
                    self.consecutive_errors.pop(layer, None)

    async def _emit_degradation(self, layer: str, reason: str) -> None:
        if self._bus is not None:
            try:
                await self._bus.emit(
                    Event(
                        type=EventType.DEGRADATION,
                        data={"layer": layer, "reason": reason},
                    )
                )
            except RuntimeError:
                logger.warning("DEGRADATION event emit failed (layer=%s): %s", layer, reason)
