"""功能：best-effort 写入失败登记与有界重试（防「写失败静默丢数据」）。

设计要点：
- **不静默**：每次失败都计数 + 结构化 warning（含通道 + 异常），并留快照供日志/指标消费
- **有界重试**：带重试回调的失败入队，下一轮 `retry_pending()` 重试；超过 `max_attempts`
  即丢弃并计入 `dropped`——保证 at-least-once 有上界，持续故障不会无限堆积内存。
  调用方传入当前轮 live 依赖（如新建的 manager）时，重试回调按该实例重放，不绑定
  注册瞬间的旧实例（旧实例可能已随上轮结束关闭）。
- **best-effort 边界**：调用方自行决定哪些写入可 best-effort；必须成功的写入（如
  checkpoint 前的会话落库）不经过本注册表，失败直接抛出（fail-closed）
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("bandi.observability.write_failures")

# 默认重试次数——下一轮补一次，再失败即丢弃（避免持续故障拖慢主链）
DEFAULT_MAX_ATTEMPTS = 1
# 最近失败快照保留条数
DEFAULT_RECENT_LIMIT = 50
# 待重试队列硬上限——持续故障下丢弃最老项并计数（防无界占内存）
DEFAULT_PENDING_LIMIT = 256
# retry_pending 未传 live 依赖的哨兵（区别于显式传 None）
_MISSING: Any = object()


class RetryDependencyMissingError(RuntimeError):
    """功能：重放依赖缺失的哨兵异常——保留 pending 不计成功/失败/丢弃。

    重试回调在当前轮没有目标通道（如本轮 memory manager 无 hierarchy）时抛出：
    数据留待后续有能力的轮次补写，不得把「没写」谎报成「已写成功」。
    """


@dataclass
class WriteFailureSnapshot:
    """功能：单次写入失败的快照（通道 / 异常 / 已重试次数 / 时间 / 目标范围）。"""

    channel: str
    error: str
    attempts: int
    timestamp: float
    scope: str = ""


@dataclass
class _PendingWrite:
    """功能：待重试项（目标通道 + 重放回调 + 已尝试次数 + 失败快照）。"""

    channel: str
    retry: Callable[..., Any]
    attempts: int
    snapshot: WriteFailureSnapshot


def _adapt_retry_callback(callback: Callable[..., Any]) -> Callable[..., Any]:
    """功能：把零参重试回调适配为可接收 live 依赖的形态。

    `retry_pending(manager)` 以 `retry(manager)` 调用回调；零参闭包（`_run_write`
    的 fn 兜底路径）直接接收实参会抛 TypeError，被误判为真实失败而秒丢数据
    （max_attempts=1 即 dropped）。登记时包装：签名无位置参数 → 丢弃实参调用；
    有位置参数或签名不可测（内置可调用等）→ 原样保留，维持既有调用语义。
    """
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return callback
    accepts_positional = any(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for parameter in parameters
    )
    if accepts_positional:
        return callback
    return lambda _manager=None: callback()


class WriteFailureRegistry:
    """功能：写入失败登记表 + 有界重试队列（线程安全）。"""

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        recent_limit: int = DEFAULT_RECENT_LIMIT,
        pending_limit: int = DEFAULT_PENDING_LIMIT,
    ) -> None:
        self._max_attempts = max(0, int(max_attempts))
        self._recent: deque[WriteFailureSnapshot] = deque(maxlen=max(1, int(recent_limit)))
        self._pending: list[_PendingWrite] = []
        self._pending_limit = max(1, int(pending_limit))
        self._lock = threading.RLock()
        self._total = 0
        self._dropped = 0

    @property
    def total(self) -> int:
        with self._lock:
            return self._total

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def record(
        self,
        channel: str,
        exc: BaseException,
        retry: Callable[..., Any] | None = None,
        scope: str = "",
    ) -> None:
        """功能：登记一次写入失败；带 retry 回调时入待重试队列（有界）。

        retry 可为零参回调（`retry_pending()` 调用）或接收 live 依赖的回调
        （`retry_pending(manager)` 调用——推荐，避免重试绑定已关闭的旧实例）；
        零参回调在登记时自动适配（`_adapt_retry_callback`），两类调用均可重放。
        scope 为写入目标 store 的稳定标识（如 SQLite 库路径）——重放时仅补写
        范围匹配项，防跨库误写。
        """
        with self._lock:
            self._total += 1
            snapshot = WriteFailureSnapshot(
                channel=channel, error=str(exc), attempts=0, timestamp=time.time(), scope=scope
            )
            self._recent.append(snapshot)
            if retry is not None:
                self._enqueue_pending(
                    _PendingWrite(channel, _adapt_retry_callback(retry), 0, snapshot)
                )
        logger.warning(
            "memory write failed [channel=%s] total=%d pending=%d: %s",
            channel,
            self.total,
            self.pending_count,
            exc,
        )

    def _enqueue_pending(self, item: _PendingWrite) -> None:
        """功能：待重试入队 + 硬上限（超限驱逐最老并计入 dropped，不静默）。"""
        self._pending.append(item)
        overflow = len(self._pending) - self._pending_limit
        if overflow <= 0:
            return
        for _ in range(overflow):
            self._pending.pop(0)
            self._dropped += 1
        logger.warning(
            "memory write retry queue over limit; dropped oldest %d (pending=%d limit=%d)",
            overflow,
            len(self._pending),
            self._pending_limit,
        )

    def retry_pending(
        self,
        manager: Any = _MISSING,
        scope_of: Callable[[str], str] | None = None,
    ) -> dict[str, int]:
        """功能：重试待重试项——成功出队，失败累计次数，超限丢弃并计数。

        显式传入 manager（当前轮 live 依赖）时，重试回调以 `retry(manager)` 调用；
        不传时按零参回调调用（兼容旧调用）。快照 attempts 随每次重试尝试更新。
        `scope_of` 给定时按通道取当前目标范围（如本轮 live 库路径）：与登记范围
        不匹配的项不重放、保留 pending（跨库安全）；回调抛
        `RetryDependencyMissingError`（本轮无该依赖）时不计数、保留 pending
        每批按通道汇总 warning 一次。
        """
        with self._lock:
            pending, self._pending = self._pending, []
        succeeded = failed = dropped = deferred = 0
        missing_by_channel: dict[str, int] = {}
        for item in pending:
            if scope_of is not None and item.snapshot.scope != scope_of(item.channel):
                # 目标范围不匹配（本轮指向别的库/目录）：保留 pending，留待目标轮次
                with self._lock:
                    self._enqueue_pending(item)
                deferred += 1
                continue
            item.attempts += 1
            try:
                if manager is _MISSING:
                    item.retry()
                else:
                    item.retry(manager)
                succeeded += 1
            except RetryDependencyMissingError:
                # 依赖缺失：本轮无能力补写——保留 pending，不计成功/失败/丢弃；
                # 未真正重放，不消耗重试预算（missing 后真实失败仍按 max_attempts 计）
                item.attempts -= 1
                with self._lock:
                    self._enqueue_pending(item)
                missing_by_channel[item.channel] = missing_by_channel.get(item.channel, 0) + 1
            except Exception as e:  # noqa: BLE001 — 重试仍失败继续走丢弃计数
                with self._lock:
                    item.snapshot.attempts = item.attempts
                if item.attempts >= max(1, self._max_attempts):
                    with self._lock:
                        self._dropped += 1
                    dropped += 1
                    logger.warning(
                        "memory write retry exhausted [channel=%s] attempts=%d dropped=%d: %s",
                        item.channel,
                        item.attempts,
                        self.dropped,
                        e,
                    )
                else:
                    with self._lock:
                        self._enqueue_pending(item)
                    failed += 1
            else:
                with self._lock:
                    item.snapshot.attempts = item.attempts
        if missing_by_channel:
            detail = ", ".join(f"{ch}={n}" for ch, n in sorted(missing_by_channel.items()))
            logger.warning(
                "memory write retry deferred; dependency missing for %d item(s) [%s]",
                sum(missing_by_channel.values()),
                detail,
            )
        return {
            "succeeded": succeeded,
            "failed": failed,
            "dropped": dropped,
            "deferred": deferred,
        }

    def snapshot(self) -> dict[str, Any]:
        """功能：失败明细快照（供日志 / 指标 / trace 消费；recent 明细锁内物化）。"""
        with self._lock:
            recent = list(self._recent)
            total, dropped, pending = self._total, self._dropped, len(self._pending)
            rows = [
                {"channel": r.channel, "error": r.error, "attempts": r.attempts} for r in recent
            ]
        return {
            "total": total,
            "dropped": dropped,
            "pending": pending,
            "channels": sorted({r.channel for r in recent}),
            "recent": rows,
        }

    def reset(self) -> None:
        """功能：清空计数与队列（测试隔离 / 会话级重置）。"""
        with self._lock:
            self._recent.clear()
            self._pending.clear()
            self._total = 0
            self._dropped = 0


_registry = WriteFailureRegistry()


def get_registry() -> WriteFailureRegistry:
    """功能：取全局写入失败注册表（进程内单例）。"""
    return _registry


def reset_registry() -> None:
    """功能：重置全局注册表（测试隔离用）。"""
    _registry.reset()
