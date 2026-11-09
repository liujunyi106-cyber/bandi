# 功能：指数退避公共实现（带抖动与上限封顶——LLM 层与工具层共用单一实现，防双实现漂移）
from __future__ import annotations

import random

# 抖动因子范围：delay *= (0.5, 1.0]——半抖动区间
_JITTER_RANGE = 0.5
_JITTER_BASE = 0.5


def compute_backoff_delay(
    base_delay: float,
    backoff_factor: float,
    attempt: int,
    max_delay: float = 30.0,
    jitter: bool = True,
) -> float:
    """功能：按指数退避计算第 attempt 次重试的等待秒数——cap 于 max_delay；
    jitter 开启时乘以 (0.5, 1.0] 随机因子打破同步重试（雷鸣群防护）。"""
    delay = min(base_delay * (backoff_factor**attempt), max_delay)
    if jitter:
        delay = delay * (_JITTER_BASE + random.random() * _JITTER_RANGE)
    return delay
