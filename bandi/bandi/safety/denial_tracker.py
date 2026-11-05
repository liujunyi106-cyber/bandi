# 功能：连续拒绝追踪——防「用户反复拒绝 → 反复追问」的空转（.2）
#
# （见 `docs/架构设计细节.md:3456` §十八-K），差距清单把 标为「✅ 已完成」
#
# * **连续**拒绝 ≥ `consecutive_threshold` → 不再追问，直接拒绝（避免空转）；
# * **累计**拒绝 ≥ `total_threshold` → 会话应中止（防止模型无意义重试）；
# * 一次批准即清零**连续**计数（累计计数保留）；
# * 记录带 TTL——过期拒绝视为失效，避免历史决策永久生效。

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# 连续拒绝达到此数即不再追问
DEFAULT_CONSECUTIVE_THRESHOLD = 3
DEFAULT_TOTAL_THRESHOLD = 10
DEFAULT_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class DenialStats:
    """某 (session, tool) 的拒绝统计。`consecutive` 自最近一次批准起算。"""

    consecutive: int = 0
    total: int = 0


class DenialTracker:
    """线程安全的拒绝计数器。key = `(session_id, tool_name)`。"""

    def __init__(
        self,
        *,
        consecutive_threshold: int = DEFAULT_CONSECUTIVE_THRESHOLD,
        total_threshold: int = DEFAULT_TOTAL_THRESHOLD,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._consecutive_threshold = max(1, int(consecutive_threshold))
        self._total_threshold = max(1, int(total_threshold))
        self._ttl = max(0.0, float(ttl_seconds))
        self._lock = threading.Lock()
        # 记录形如 (单调序号, 时间戳)：序号定「批准前后」的先后（time.time() 同 tick
        self._seq = 0
        self._denials: dict[tuple[str, str], list[tuple[int, float]]] = {}
        self._approved_at: dict[tuple[str, str], tuple[int, float]] = {}

    # ---------- 记账 ----------

    def record_denial(self, session_id: str, tool_name: str) -> DenialStats:
        key = (session_id, tool_name)
        with self._lock:
            stamps = self._prune(key)
            self._seq += 1
            stamps.append((self._seq, time.time()))
            self._denials[key] = stamps
            return self._stats_unlocked(key)

    def record_approval(self, session_id: str, tool_name: str) -> None:
        """一次批准清零**连续**计数（累计保留）——否则「偶尔批准」也会被永久压制。

        以**批准序号**记录基准：连续计数 = 批准之后的拒绝数。旧实现按「批准时
        记录条数」记账，批准前的旧拒绝过期被裁剪时基准不随之收敛，连续计数被
        永久低估（破坏者审查实锤，TTL 场景下多追问阈值-1 次）。
        """
        key = (session_id, tool_name)
        with self._lock:
            self._prune(key)
            self._seq += 1
            self._approved_at[key] = (self._seq, time.time())

    # ---------- 查询 ----------

    def stats(self, session_id: str, tool_name: str) -> DenialStats:
        key = (session_id, tool_name)
        with self._lock:
            self._prune(key)
            return self._stats_unlocked(key)

    def should_auto_deny(self, session_id: str, tool_name: str) -> bool:
        """连续拒绝已超阈值 → 不应再发起追问（原 company 实现的 `predict` DENY 语义）。"""
        return self.stats(session_id, tool_name).consecutive >= self._consecutive_threshold

    def should_abort(self, session_id: str, tool_name: str) -> bool:
        """累计拒绝已超阈值 → 会话应中止。"""
        return self.stats(session_id, tool_name).total >= self._total_threshold

    def reset(self) -> None:
        with self._lock:
            self._denials.clear()
            self._approved_at.clear()

    # ---------- 内部 ----------

    def _prune(self, key: tuple[str, str]) -> list[tuple[int, float]]:
        """丢弃过期记录。调用方须已持锁。"""
        now = time.time()
        stamps = [entry for entry in self._denials.get(key, []) if now - entry[1] <= self._ttl]
        approved = self._approved_at.get(key)
        if approved is not None and now - approved[1] > self._ttl:
            # 批准记录同样受 TTL 约束——过期后不再作为连续计数基准
            self._approved_at.pop(key, None)
            approved = None
        if stamps:
            self._denials[key] = stamps
        else:
            self._denials.pop(key, None)
            if approved is None:
                self._approved_at.pop(key, None)
        return stamps

    def _stats_unlocked(self, key: tuple[str, str]) -> DenialStats:
        stamps = self._denials.get(key, [])
        total = len(stamps)
        approved = self._approved_at.get(key)
        if approved is None:
            return DenialStats(consecutive=total, total=total)
        consecutive = sum(1 for seq, _ts in stamps if seq > approved[0])
        return DenialStats(consecutive=max(0, consecutive), total=total)


_tracker: DenialTracker | None = None
_tracker_lock = threading.Lock()


def get_denial_tracker() -> DenialTracker:
    """进程级共享实例（测试由 conftest 重置，防跨用例串扰）。"""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = DenialTracker()
        return _tracker


def reset_denial_tracker() -> None:
    global _tracker
    with _tracker_lock:
        if _tracker is not None:
            _tracker.reset()
        _tracker = None
