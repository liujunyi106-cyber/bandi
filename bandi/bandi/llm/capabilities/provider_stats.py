# 功能：Provider 能力统计（成功率/延迟/可靠性持久化）
"""Provider 性能存储 — 多任务 fallback 的生产反馈。

记录每次生成尝试的实时结果（成功/失败、延迟，可选感知质量分），按 ``(task, provider)``
键控。:class:`CapabilityRouter` 用派生的 *reliability* 分数偏置同成本档内的候选排序
让真实流量上表现好的 Provider 优于失败或低质量的。轻量进程内 bandit，无外部存储。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

__all__ = ["ProviderStat", "ProviderStatsStore", "get_provider_stats", "reset_provider_stats"]

# 滚动质量均值的 EMA 平滑因子。
_QUALITY_ALPHA = 0.3
# 从未观测过的 Provider 的中性可靠度 — 在真实数据积累前保持排序稳定。
NEUTRAL_SCORE = 0.5


@dataclass
class ProviderStat:
    """单个 ``(task, provider)`` 对的滚动性能计数。"""

    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    quality_ema: float | None = None
    samples: int = 0

    @property
    def attempts(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 1.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.successes if self.successes else 0.0

    def reliability(self) -> float:
        """0..1 分数，综合成功率与（可选的）质量。

        无质量信号时为原始成功率；观测到质量后两者混合，使「成功但产出差」的
        Provider 排在「可靠且高质量」的之后。
        """
        if self.attempts == 0:
            return NEUTRAL_SCORE
        if self.quality_ema is None:
            return self.success_rate
        return 0.5 * self.success_rate + 0.5 * self.quality_ema

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes": self.successes,
            "failures": self.failures,
            "attempts": self.attempts,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "quality_ema": round(self.quality_ema, 4) if self.quality_ema is not None else None,
            "reliability": round(self.reliability(), 4),
            "samples": self.samples,
        }


class ProviderStatsStore:
    """线程安全的每 Provider 性能内存存储。"""

    def __init__(self) -> None:
        self._stats: dict[tuple[str, str], ProviderStat] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(task: str, provider: str) -> tuple[str, str]:
        return (str(task), str(provider))

    def record_attempt(
        self,
        task: str,
        provider: str,
        *,
        success: bool,
        latency_ms: float | None = None,
    ) -> None:
        key = self._key(task, provider)
        with self._lock:
            stat = self._stats.setdefault(key, ProviderStat())
            if success:
                stat.successes += 1
                if latency_ms is not None:
                    stat.total_latency_ms += max(0.0, float(latency_ms))
            else:
                stat.failures += 1

    def record_quality(self, task: str, provider: str, score: float) -> None:
        """把感知/评测质量分（0..1）折入滚动均值。"""
        try:
            value = max(0.0, min(float(score), 1.0))
        except (TypeError, ValueError):
            return
        key = self._key(task, provider)
        with self._lock:
            stat = self._stats.setdefault(key, ProviderStat())
            if stat.quality_ema is None:
                stat.quality_ema = value
            else:
                stat.quality_ema = (1 - _QUALITY_ALPHA) * stat.quality_ema + _QUALITY_ALPHA * value
            stat.samples += 1

    def reliability(self, task: str, provider: str) -> float:
        with self._lock:
            stat = self._stats.get(self._key(task, provider))
            return stat.reliability() if stat is not None else NEUTRAL_SCORE

    def get(self, task: str, provider: str) -> ProviderStat | None:
        with self._lock:
            stat = self._stats.get(self._key(task, provider))
            return ProviderStat(**vars(stat)) if stat is not None else None

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {f"{t}:{p}": s.to_dict() for (t, p), s in self._stats.items()}

    def clear(self) -> None:
        with self._lock:
            self._stats.clear()


_GLOBAL_STORE: ProviderStatsStore | None = None


def get_provider_stats() -> ProviderStatsStore:
    """返回进程级 provider stats 存储（懒创建单例）。"""
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        _GLOBAL_STORE = ProviderStatsStore()
    return _GLOBAL_STORE


def reset_provider_stats() -> None:
    """重置全局存储（测试隔离）。"""
    if _GLOBAL_STORE is not None:
        _GLOBAL_STORE.clear()
