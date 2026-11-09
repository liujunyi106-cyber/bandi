# 功能：进程级可观测计数注册表——跨实例聚合「不静默丢弃」类计数，供既有出口暴露
#
# 封闭在实例属性/模块私有 dict 中，无生产消费方（值班侧不可见）。本模块提供
# 最小进程级注册表（多 bus/store 实例共享一份），由既有 /health 出口读取。

from __future__ import annotations

import threading

# 事件持久化队列满被丢弃的计数键（EventBus，进程级聚合）
COUNTER_EVENT_BUS_PERSIST_DROPPED = "event_bus.persist_dropped"
# long_term 维度漏传计数键前缀（后缀 = project_id / employee_id）
COUNTER_LONG_TERM_SCOPE_MISS_PREFIX = "memory.long_term.scope_strict_miss."

_lock = threading.Lock()
_counts: dict[str, int] = {}


def scope_miss_counter_name(dimension: str) -> str:
    """功能：维度漏传计数键名（键名即观测契约，不得随意变更）。"""
    return f"{COUNTER_LONG_TERM_SCOPE_MISS_PREFIX}{dimension}"


def increment(name: str, amount: int = 1) -> int:
    """功能：累加进程级计数并返回新值（多实例共享，线程安全；允许负数用于借还）。"""
    with _lock:
        value = _counts.get(name, 0) + int(amount)
        _counts[name] = value
        return value


def snapshot() -> dict[str, int]:
    """功能：读取全部计数快照（副本——调用方修改不影响注册表）。"""
    with _lock:
        return dict(_counts)


def reset() -> None:
    """功能：清空计数（测试隔离 / 进程级重置用）。"""
    with _lock:
        _counts.clear()


__all__ = [
    "COUNTER_EVENT_BUS_PERSIST_DROPPED",
    "COUNTER_LONG_TERM_SCOPE_MISS_PREFIX",
    "increment",
    "reset",
    "scope_miss_counter_name",
    "snapshot",
]
