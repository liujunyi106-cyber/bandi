# 功能：工具结果缓存（ToolResultCache 按签名缓存）
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bandi.core.message import ToolResult

logger = logging.getLogger(__name__)

_CacheEntry = tuple[float, ToolResult]

DEFAULT_MAXSIZE = 512


@dataclass
class _InflightFailure:
    """功能：单飞批内共享的异常槽——等待者重抛同一异常，不入缓存。"""

    error: BaseException


class ToolResultCache:
    """工具结果缓存：LRU + TTL + 线程安全 + 同 key 单飞。

    并发模型：
    - 结构体（`_cache`/`_tool_keys`/`_inflight*`）由 `threading.RLock` 保护——
      工具并发分区可能跨线程调用；用**可重入**锁是因为 `get`/`has` 内部会调
      `_discard`（同样持锁），普通 Lock 会自死锁。
    - 同 key 并发未命中由 `_inflight` 的 asyncio.Lock 串行化，保证只计算一次
      （防缓存击穿/stampede）；不同 key 互不阻塞。无等待者时清理锁表，防止无界增长。
    """

    def __init__(self, maxsize: int = DEFAULT_MAXSIZE, default_ttl: float = 60.0):
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._tool_keys: dict[str, list[str]] = {}
        # 缓存键 → 写入时 scope（executor 身份切换按旧前缀清理；直接调用方可不传）
        self._key_scopes: dict[str, str] = {}
        self._lock = threading.RLock()
        self._inflight: dict[str, asyncio.Lock] = {}
        self._inflight_users: dict[str, int] = {}
        # 单飞批内共享结果（成败都广播给等待者）——只存内存不落缓存：
        # 失败结果/异常不得进缓存（与直连路径语义一致），但同一并发批的等待者
        # 必须复用赢家那次执行，不得重复执行（修复 / 异常同样共享）
        self._inflight_results: dict[str, ToolResult | _InflightFailure] = {}

    @staticmethod
    def build_key(tool_name: str, arguments: dict[str, Any], scope: str = "") -> str:
        # 不同上下文同 tool+args 不得共享缓存；scope 为空保持旧键语义（向后兼容）
        raw = f"{tool_name}:{json.dumps(arguments, sort_keys=True, default=str)}"
        if scope:
            raw = f"{scope}:{raw}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> ToolResult | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, result = entry
            if time.monotonic() > expires_at:
                self._discard(key)
                return None
            self._cache.move_to_end(key)
            return result

    def set(self, key: str, result: ToolResult, ttl: float | None = None, scope: str = "") -> None:
        # 功能：写入缓存条目（ttl 缺省用默认值；scope 供身份切换按前缀清理）。
        # 🔟 冗余缓存双写（非 bug）：executor 成功路径既经单飞内写、又有直写兜底
        # 两次写入幂等——保留 cache 侧 set 作为直接调用方（不走单飞 API）的语义入口。
        with self._lock:
            while len(self._cache) >= self._maxsize:
                # 逐条 LRU 淘汰（`while` 而非 `if`：maxsize 调小时可一次收敛到上限内）
                evicted_key = next(iter(self._cache))
                self._discard(evicted_key)
            expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
            self._cache[key] = (expires_at, result)
            if scope:
                self._key_scopes[key] = scope
            if result.tool_name:
                # invalidate_tool 重复 pop（条目无法按预期失效/计数错乱）
                keys = self._tool_keys.setdefault(result.tool_name, [])
                if key not in keys:
                    keys.append(key)

    def has(self, key: str) -> bool:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return False
            expires_at, _ = entry
            if time.monotonic() > expires_at:
                self._discard(key)
                return False
            return True

    async def get_or_compute(
        self,
        key: str,
        factory: Callable[[], Awaitable[ToolResult]],
        ttl: float | None = None,
        scope: str = "",
    ) -> ToolResult:
        """功能：命中即返回；未命中则**同 key 单飞**计算一次——成功结果写入缓存
        失败结果/异常只在本并发批内广播（不入缓存，后续新调用可重试重算）。

        单飞语义：同 key 的并发调用只会执行一次 `factory`，其余等待并复用结果
        （防缓存击穿）。`factory` 抛异常时异常广播给同批等待者（不重算）
        锁正常释放，后续调用可重试——不留死锁。
        """
        result, _ = await self.get_or_compute_with_status(key, factory, ttl=ttl, scope=scope)
        return result

    async def get_or_compute_with_status(
        self,
        key: str,
        factory: Callable[[], Awaitable[ToolResult]],
        ttl: float | None = None,
        scope: str = "",
    ) -> tuple[ToolResult, bool]:
        """功能：同 `get_or_compute`，额外返回 `computed`——本次调用是否真的执行了 factory。

        `computed=True` = 本调用是单飞赢家（结果带自己的标识/事件已由调用方产生）；

        修复：成功结果才入缓存；失败结果只在本单飞批内广播给等待者
        （同 key 并发只执行一次，无论成败；后续新调用因缓存无键会重新执行）。
        修复：factory 抛异常同样写入批内共享槽——等待者重抛同一异常而不是
        各自重跑 factory（异常不入缓存，槽清空后新调用可重试）。
        """
        cached = self.get(key)
        if cached is not None:
            return cached, False

        lock = self._acquire_inflight(key)
        try:
            async with lock:
                cached = self.get(key)  # 双重检查：等待期间可能已被前者写入
                if cached is not None:
                    return cached, False
                with self._lock:
                    shared = self._inflight_results.get(key)
                if isinstance(shared, _InflightFailure):
                    # 赢家执行抛异常——等待者收到同源异常，不重复执行
                    raise shared.error
                if shared is not None:
                    # 赢家本次执行失败（不入缓存）——等待者复用该结果，不重复执行
                    return shared, False
                try:
                    result = await factory()
                except BaseException as exc:  # 异常广播给批内等待者（含取消）
                    with self._lock:
                        self._inflight_results[key] = _InflightFailure(exc)
                    raise
                if result.success:
                    self.set(key, result, ttl, scope=scope)
                else:
                    with self._lock:
                        self._inflight_results[key] = result
                return result, True
        finally:
            self._release_inflight(key)

    def _acquire_inflight(self, key: str) -> asyncio.Lock:
        with self._lock:
            lock = self._inflight.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._inflight[key] = lock
            self._inflight_users[key] = self._inflight_users.get(key, 0) + 1
            return lock

    def _release_inflight(self, key: str) -> None:
        with self._lock:
            remaining = self._inflight_users.get(key, 1) - 1
            if remaining <= 0:
                # 无等待者即摘除，防止锁表随 key 数量无界增长；批内共享结果随之清理
                self._inflight.pop(key, None)
                self._inflight_users.pop(key, None)
                self._inflight_results.pop(key, None)
            else:
                self._inflight_users[key] = remaining

    def invalidate(self, key: str) -> None:
        with self._lock:
            if key in self._cache:
                self._discard(key)

    def invalidate_tool(self, tool_name: str) -> None:
        with self._lock:
            keys = self._tool_keys.pop(tool_name, [])
            for k in keys:
                self._cache.pop(k, None)
                self._key_scopes.pop(k, None)

    def purge_scope_prefix(self, prefix: str) -> int:
        """功能：清理指定 scope 前缀的全部缓存条目（身份切换后旧 scope 不得命中）。

        `executor.set_agent_id` 切换身份时按旧 base scope 前缀调用；未记录 scope
        的键（直接调用方）不受影响。返回被清理条数。
        """
        if not prefix:
            return 0
        with self._lock:
            doomed = [key for key, scope in self._key_scopes.items() if scope.startswith(prefix)]
            for key in doomed:
                self._discard(key)
            return len(doomed)

    def _discard(self, key: str) -> None:
        self._cache.pop(key, None)
        self._key_scopes.pop(key, None)
        self._cleanup_index(key)

    def _cleanup_index(self, key: str) -> None:
        for keys in self._tool_keys.values():
            if key in keys:
                keys.remove(key)
                break

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._tool_keys.clear()
            self._key_scopes.clear()

    @property
    def size(self) -> int:
        with self._lock:
            _now = time.monotonic()
            expired = [k for k, (t, _) in self._cache.items() if t <= _now]
            for k in expired:
                self._discard(k)
            return len(self._cache)
