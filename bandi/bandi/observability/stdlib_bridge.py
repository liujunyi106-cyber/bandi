# 功能：stdlib logging → 自研 JSON Logger 的桥——让主链（loop/states 等）的日志也进结构化管道
#
# 设计要点（为什么不是简单地在 Handler.emit 里直接调 Logger._log）：
# 1. **不阻塞调用方**：主链日志点可能在热路径上，队满即丢弃 + 计数，绝不 await/阻塞；
# 2. **上下文跨线程携带**：Logger 的落盘在独立消费线程，而 ContextVar 不跨线程继承
# 因此在 emit（调用方上下文）中就把上下文快照下来随条目一起入队；
# 3. **自身异常必须吞掉**：日志桥是观测设施，任何故障都不得穿透到业务代码；
# 4. **消息立即格式化**：LogRecord 的 args 可能是可变对象，延迟格式化会读到已变的值。

from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass

from bandi.observability.context import LogContext, current_log_context
from bandi.observability.logger import Logger, LogLevel

DEFAULT_MAXSIZE = 10000

# close() 等待消费线程排空的兜底时限（秒）——绝不无限等。
_CLOSE_TIMEOUT_SECONDS = 3.0

# 安装标记属性名（挂在 logging.Logger 实例上，用于幂等装/卸）。
_BRIDGE_ATTR = "_bandi_logger_bridge"

_LEVEL_MAP: dict[int, LogLevel] = {
    logging.DEBUG: LogLevel.DEBUG,
    logging.INFO: LogLevel.INFO,
    logging.WARNING: LogLevel.WARNING,
    logging.ERROR: LogLevel.ERROR,
    logging.CRITICAL: LogLevel.CRITICAL,
}


@dataclass(frozen=True)
class BridgeStats:
    """桥的计数快照。enqueued + dropped 恒等于收到的日志条数。"""

    enqueued: int = 0
    dropped: int = 0
    written: int = 0


@dataclass(frozen=True)
class _Payload:
    level: LogLevel
    module: str
    message: str
    exc_text: str
    ctx: LogContext


class LoggerBridgeHandler(logging.Handler):
    """把 stdlib 日志转发进自研 Logger 的有界队列处理器。"""

    def __init__(
        self,
        logger: Logger,
        *,
        logger_name: str,
        maxsize: int = DEFAULT_MAXSIZE,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self.logger_name = logger_name
        self._logger = logger
        self._maxsize = max(1, int(maxsize))
        self._queue: queue.Queue[_Payload | None] = queue.Queue(maxsize=self._maxsize)
        self._lock = threading.Lock()
        self._enqueued = 0
        self._dropped = 0
        self._written = 0
        self._stopped = threading.Event()
        self._prev_propagate: bool | None = None
        self._prev_level: int | None = None
        self._thread = threading.Thread(
            target=self._consume_loop, name="bandi-log-bridge", daemon=True
        )
        self._thread.start()

    # ---------- 对外 ----------

    def stats(self) -> BridgeStats:
        with self._lock:
            return BridgeStats(
                enqueued=self._enqueued, dropped=self._dropped, written=self._written
            )

    # ---------- logging.Handler ----------

    def emit(self, record: logging.LogRecord) -> None:
        """**必须在调用方上下文内同步完成上下文快照**，且绝不阻塞。"""
        if self._stopped.is_set():
            # 停止后进入的日志计入 dropped（统计口径：enqueued+dropped=收到条数）
            self._bump("dropped")
            return
        try:
            level = _LEVEL_MAP.get(record.levelno)
            if level is None:
                level = LogLevel.ERROR if record.levelno >= logging.ERROR else LogLevel.INFO
            exc_text = ""
            if record.exc_info:
                exc_text = "".join(traceback.format_exception(*record.exc_info))
            payload = _Payload(
                level=level,
                module=record.name,
                message=record.getMessage(),
                exc_text=exc_text,
                ctx=current_log_context(),
            )
        except Exception:  # noqa: BLE001 — 构造失败只丢弃该条，不影响主链
            self._bump("dropped")
            return

        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._bump("dropped")
            return
        self._bump("enqueued")

    def close(self) -> None:
        """停止接收 → 唤醒消费线程 → 限时排空。可重复调用。"""
        self._stopped.set()
        self._push_sentinel()
        if self._thread.is_alive():
            self._thread.join(timeout=_CLOSE_TIMEOUT_SECONDS)
        super().close()

    # ---------- 内部 ----------

    def _bump(self, field: str) -> None:
        with self._lock:
            if field == "enqueued":
                self._enqueued += 1
            elif field == "dropped":
                self._dropped += 1
            else:
                self._written += 1

    def _push_sentinel(self) -> None:
        deadline = time.monotonic() + _CLOSE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                self._queue.put_nowait(None)
                return
            except queue.Full:
                if not self._thread.is_alive():
                    return
                time.sleep(0.005)

    def _consume_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self._stopped.is_set():
                    return
                continue
            if item is None:
                return
            self._write(item)

    def _write(self, payload: _Payload) -> None:
        ctx = payload.ctx
        extra: dict[str, object] = {
            "session_id": ctx.session_id,
            "turn_index": ctx.turn_index,
            "agent_id": ctx.agent_id,
            "parent_agent_id": ctx.parent_agent_id,
            "role": ctx.role,
            "run_id": ctx.run_id,
            "request_id": ctx.request_id,
            "user_id": ctx.user_id,
        }
        if payload.exc_text:
            extra["exc_text"] = payload.exc_text
        try:
            self._logger._log(payload.level, payload.module, payload.message, **extra)
        except Exception:  # noqa: BLE001 — 观测设施故障绝不穿透；计入 dropped 以便暴露
            self._bump("dropped")
            return
        self._bump("written")


def install_logger_bridge(
    logger: Logger,
    *,
    logger_name: str = "bandi",
    maxsize: int = DEFAULT_MAXSIZE,
    level: int = logging.NOTSET,
    logger_level: int = logging.DEBUG,
) -> LoggerBridgeHandler:
    """把桥挂到指定 logger 上（幂等：已装且未关闭则复用同一个 handler）。

    两处 level 的分工（这是本模块最容易踩的坑）：
      - **目标 logger 的 level 必须放开**（默认 DEBUG）。stdlib 的 `Logger.info()` 先过
        `isEnabledFor`，而本仓全仓 `logging.getLogger(__name__)` 且从未配置 root ——
        生效 level 继承 root 的 WARNING，导致主链的 INFO/DEBUG 在到达 handler 前就被丢弃。
      - **真正决定「哪些进 JSONL」的是 `Logger` 自身的 `_level`**（默认 INFO）。
        即：桥负责「收得到」，Logger 负责「留哪些」。过滤口径单一，不两处打架。

    `propagate` 置 False——桥的意义就是接管该 logger 的落盘，继续向 root 冒泡会重复输出。
    """
    target = logging.getLogger(logger_name)
    existing = getattr(target, _BRIDGE_ATTR, None)
    if isinstance(existing, LoggerBridgeHandler) and not existing._stopped.is_set():
        # 跨 run 复用同一 handler，但目标 Logger 每 run 重建（agent_runner）——
        # 必须重绑到传入实例，否则桥继续写上一轮已关闭的 Logger（run2 起静默丢日志）。
        existing._logger = logger
        return existing

    handler = LoggerBridgeHandler(logger, logger_name=logger_name, maxsize=maxsize, level=level)
    handler._prev_propagate = target.propagate
    handler._prev_level = target.level
    target.addHandler(handler)
    target.propagate = False
    target.setLevel(logger_level)
    setattr(target, _BRIDGE_ATTR, handler)
    return handler


def uninstall_logger_bridge(handler: LoggerBridgeHandler) -> None:
    """摘掉桥并恢复 propagate / level。对未安装/已摘除的 handler 调用是安全的空操作。"""
    target = logging.getLogger(handler.logger_name)
    if handler in target.handlers:
        target.removeHandler(handler)
    if getattr(target, _BRIDGE_ATTR, None) is handler:
        try:
            delattr(target, _BRIDGE_ATTR)
        except AttributeError:
            pass
    if handler._prev_propagate is not None:
        target.propagate = handler._prev_propagate
        handler._prev_propagate = None
    if handler._prev_level is not None:
        target.setLevel(handler._prev_level)
        handler._prev_level = None
