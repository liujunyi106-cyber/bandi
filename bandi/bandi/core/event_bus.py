# 功能：事件总线（订阅/发布/持久化 + 全局引导）
from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import json
import logging
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from io import TextIOWrapper
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .events import Event, EventType

if TYPE_CHECKING:
    from bandi.core.dependencies import Dependencies

logger = logging.getLogger(__name__)

_PERSIST_FLUSH_BATCH = 10
_PERSIST_ROTATE_BYTES = 10 * 1024 * 1024
# B5c 8.11：旋转保留代数（.1/.2/.3——原仅留一代 .1，长期历史不可复盘）
_PERSIST_ROTATE_KEEP = 3
_MAX_SEEN_EVENT_IDS = 100_000
_FORCE_FLUSH_TYPES = {
    EventType.TERMINATED,
    EventType.TOOL_COMPLETED,
    EventType.TOOL_ERROR,
    EventType.STATE_CHANGED,
}
# 有界队列 + 丢弃计数语义）
_PERSIST_QUEUE_MAXSIZE = 10_000
_PERSIST_DRAIN_TIMEOUT_MS = 2000
_PERSIST_DRAIN_POLL_S = 0.005
_PERSIST_SENTINEL_RETRY = 3
_PERSIST_SENTINEL_RETRY_MS = 200
_PERSIST_THREAD_JOIN_TIMEOUT_S = 2.0
_SOURCING_FIELDS = (
    "trace_id",
    "session_id",
    "turn_id",
    "attempt_id",
    "parent_event_id",
    "command_id",
    "schema_version",
    "source",
)

if sys.platform == "win32":
    import msvcrt

    def _lock_byte(f: TextIOWrapper) -> None:
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_byte(f: TextIOWrapper) -> None:
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_byte(f: TextIOWrapper) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    def _unlock_byte(f: TextIOWrapper) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class _EventContext:  # 功能：事件上下文（ContextVar 承载，emit 自动补溯源字段）
    __slots__ = (
        "trace_id",
        "session_id",
        "turn_id",
        "attempt_id",
        "parent_event_id",
        "command_id",
        "source",
    )

    def __init__(self) -> None:
        self.trace_id = ""
        self.session_id = ""
        self.turn_id = ""
        self.attempt_id = ""
        self.parent_event_id = ""
        self.command_id = ""
        self.source = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "attempt_id": self.attempt_id,
            "parent_event_id": self.parent_event_id,
            "command_id": self.command_id,
            "source": self.source,
        }


_event_ctx_var: contextvars.ContextVar[_EventContext | None] = contextvars.ContextVar(
    "bandi_event_ctx", default=None
)


def set_event_context(**fields: str) -> contextvars.Token[_EventContext | None]:
    """设置当前执行上下文的事件溯源字段（返回 token 供 reset 恢复）。

    RF-18 审查修复：每次复制新对象再 set——原实现就地修改 `_event_ctx_var.get()`
    返回的共享 ctx；reset(token) 恢复的是同一对象引用（字段已被永久改写）
    导致「set(session_id=X) + reset」仍把 X 泄漏给父上下文/后续事件。
    """
    current = _event_ctx_var.get()
    ctx = _EventContext()
    if current is not None:
        for k in _EventContext.__slots__:
            setattr(ctx, k, getattr(current, k, "") or "")
    for k, v in fields.items():
        if hasattr(ctx, k):
            setattr(ctx, k, v or "")
    return _event_ctx_var.set(ctx)


def reset_event_context(token: contextvars.Token[_EventContext | None]) -> None:
    _event_ctx_var.reset(token)


class ReplayError:  # 功能：只读重放审计的单条错误记录（行号 + 错误类型 + 详情）
    __slots__ = ("line_no", "kind", "detail")

    def __init__(self, line_no: int, kind: str, detail: str) -> None:
        self.line_no = line_no
        self.kind = kind
        self.detail = detail


class EventReplayReport:  # 功能：事件日志只读重放校验报告（hash 链/sequence/event_id 完整性）
    __slots__ = (
        "ok",
        "lines_read",
        "valid_lines",
        "first_sequence",
        "last_sequence",
        "hash_chain_broken",
        "continues_from_previous",
        "duplicate_event_ids",
        "errors",
    )

    def __init__(self) -> None:
        self.ok = True
        self.lines_read = 0
        self.valid_lines = 0
        self.first_sequence = 0
        self.last_sequence = 0
        self.hash_chain_broken = False
        self.continues_from_previous = False
        self.duplicate_event_ids: list[str] = []
        self.errors: list[ReplayError] = []

    def _record(self, line_no: int, kind: str, detail: str) -> None:
        self.ok = False
        self.errors.append(ReplayError(line_no, kind, detail))


class EventBus:  # 功能：发布-订阅事件总线，解耦 Agent 各阶段与外部监听器（含可选文件持久化）
    def __init__(
        self,
        persist_path: str | Path | None = None,
        fail_fast_on_handler_error: bool = False,
    ):
        self._handlers: dict[EventType, list[Callable[[Event], Coroutine[Any, Any, None]]]] = {}
        self._lock = asyncio.Lock()
        self._emit_lock = asyncio.Lock()
        self._sequence = 0
        self._persist_path = Path(persist_path) if persist_path else None
        self._persist_file: TextIOWrapper | None = None
        self._persist_enabled = False
        self._persist_pending = 0
        # 自计写入字节（rotate 判断用——tell() 会触发 TextIOWrapper 内部 flush）
        self._written_bytes = 0
        self._last_hash: str = ""
        # B5c 8.11：滑动窗口（deque 逐出最旧）——原超限整集 clear 造成幂等断层
        # （清空后近期 id 可再次通知）
        self._seen_event_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        # 落盘去重（仅 persist 路径维护——与进程内去重职责分离，首次落盘不受影响）；
        self._persisted_event_ids: set[str] = set()
        self._persisted_order: deque[str] = deque()
        self._fail_fast_on_handler_error = fail_fast_on_handler_error
        self._handler_error_count = 0
        self._persist_queue: queue.Queue[dict[str, Any] | None] | None = None
        self._persist_thread: threading.Thread | None = None
        self._persist_dropped = 0
        # 第四轮 持久化代数——每次 enable 递增；旧代写线程的记录按代数校验丢弃
        # 防 disable 超时后旧线程存活并读到新代文件造成跨代并发写（hash 链/计数竞态）
        self._persist_generation = 0
        self._persist_stale_thread: threading.Thread | None = None
        self._persist_state_lock = threading.Lock()

    @property
    def handler_error_count(self) -> int:
        return self._handler_error_count

    @property
    def persist_dropped(self) -> int:
        """功能：因队列满被丢弃的持久化事件数（有界丢弃计数）。"""
        return self._persist_dropped

    def _account_dropped(self, count: int) -> None:
        """功能：丢弃计数单源——实例自计数与进程级观测注册表同步。"""
        if count <= 0:
            return
        self._persist_dropped += count
        from bandi.observability.counters import (
            COUNTER_EVENT_BUS_PERSIST_DROPPED,
            increment,
        )

        increment(COUNTER_EVENT_BUS_PERSIST_DROPPED, count)

    def enable_persistence(self) -> None:
        if self._persist_path is None or self._persist_enabled:
            return
        stale_thread = self._persist_stale_thread
        self._persist_stale_thread = None
        with self._persist_state_lock:
            self._persist_generation += 1
            generation = self._persist_generation
        if stale_thread is not None and stale_thread.is_alive():
            # 旧代写线程未退出：新代递增使其记录被代数校验丢弃（不跨代并发写当前文件）
            logger.warning(
                "EventBus stale persist worker still alive; advanced to generation %d",
                generation,
            )
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        if self._persist_path.exists():
            # sequence/hash/去重集从最近完好行恢复
            lines = self._persist_path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                try:
                    obj = json.loads(line)
                    self._sequence = int(obj.get("sequence", 0) or 0)
                    self._last_hash = hashlib.sha256(line.encode("utf-8")).hexdigest()
                    if obj.get("event_id"):
                        self._persisted_event_ids.add(obj["event_id"])
                        self._persisted_order.append(obj["event_id"])
                        # 恢复 id 永不可逐出，set/deque 失同步）
                        if obj["event_id"] not in self._seen_event_ids:
                            self._seen_event_ids.add(obj["event_id"])
                            self._seen_order.append(obj["event_id"])
                    break
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
            else:
                logger.warning("EventBus sequence recovery failed; starting from zero")
        persist_file = self._persist_path.open("a", encoding="utf-8")
        with self._persist_state_lock:
            self._persist_file = persist_file
        # 功能：按现有文件大小初始化本代已写字节——进程内残留计数不得造成首个事件误 rotation
        try:
            self._written_bytes = self._persist_path.stat().st_size
        except OSError:
            self._written_bytes = 0
        self._persist_enabled = True
        # 实例（不再每轮重读 self._persist_queue，防换队列后哨兵无人消费/跨队列消费）；
        persist_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=_PERSIST_QUEUE_MAXSIZE
        )
        self._persist_queue = persist_queue
        self._persist_thread = threading.Thread(
            target=self._persist_worker,
            args=(persist_queue, generation),
            name="bandi-event-persist",
            daemon=True,
        )
        self._persist_thread.start()

    def disable_persistence(self, timeout_ms: int | None = None) -> None:
        """功能：停止持久化——先停新入队，再有界排空快照队列后关文件。

        排空成功塞 None 哨兵停写线程；写线程超时未停则把未完成条数计入
        `persist_dropped` 并 warning（不静默丢积压）。重复调用幂等。

        🟡-5 修复：① 排空/join **之后**递增 `_persist_generation`
        ——原实现只在 enable 递增，导致 disable 后仍在飞的旧 worker 能通过二次校验
        继续写；**必须在排空之后**递增，否则会先让当前 worker 丢弃待排空的尾批；
        ② final `flush/close` 移入 `_persist_state_lock`——原实现在锁外关闭
        join 超时（worker 仍存活）时会与锁内写段竞态，worker 抛
        `I/O operation on closed file` 且尾批静默丢失。
        """
        self._persist_enabled = False
        pending_queue, thread = self._persist_queue, self._persist_thread
        self._persist_queue, self._persist_thread = None, None
        if pending_queue is not None:
            timeout = _PERSIST_DRAIN_TIMEOUT_MS if timeout_ms is None else max(0, int(timeout_ms))
            self._drain_blocking(timeout, pending_queue, thread)
            sentinel_queued = False
            # worker 处理完积压后会永久阻塞在 get()（线程泄漏）。此处有界补试。
            for _ in range(_PERSIST_SENTINEL_RETRY):
                try:
                    pending_queue.put_nowait(None)
                    sentinel_queued = True
                    break
                except queue.Full:
                    self._drain_blocking(_PERSIST_SENTINEL_RETRY_MS, pending_queue, thread)
            if not sentinel_queued:
                logger.warning(
                    "EventBus persist sentinel not queued (queue full) — worker may linger"
                )
            if thread is not None:
                thread.join(timeout=_PERSIST_THREAD_JOIN_TIMEOUT_S)
                if thread.is_alive():
                    # 旧线程仍存活：记引用供 enable 告警 + 新代校验兜底
                    self._persist_stale_thread = thread
            # 🟡-2：统一判定——无论写线程死活都统计未消费积压（worker 被
            # 队列积压静默丢）。哨兵仍在队列（未消费）时 unfinished 含它，扣减后
            # 才是事件条数；已消费/在途哨兵不扣减。
            # 估算上界（含在途 1 条）：unfinished 覆盖「已取走但未 task_done」条目
            # 关闭窗口内宁可多报 1 条也不静默丢。
            sentinel_waiting = False
            if sentinel_queued:
                with pending_queue.mutex:
                    sentinel_waiting = any(item is None for item in pending_queue.queue)
            remaining = pending_queue.unfinished_tasks - (1 if sentinel_waiting else 0)
            if remaining > 0:
                self._account_dropped(remaining)
                logger.warning("EventBus persist backlog dropped on close: %d event(s)", remaining)
        # 与写段（`_persist_one` 的锁内 write）互斥：关文件期间旧 worker 不可能在写。
        # 代数在此递增（排空之后）——仍在飞的 worker 从此写入一律被丢弃
        with self._persist_state_lock:
            self._persist_generation += 1
            if self._persist_file is not None and not self._persist_file.closed:
                self._persist_file.flush()
                self._persist_file.close()
            self._persist_pending = 0

    def _drain_blocking(
        self,
        timeout_ms: int,
        persist_queue: queue.Queue[dict[str, Any] | None],
        worker: threading.Thread | None = None,
    ) -> bool:
        """功能：阻塞等待显式队列排空（毫秒上界），返回是否已排空。

        写线程已死时立即返回——否则 `unfinished_tasks` 永不归零
        每次 drain 都白等满超时（关闭路径被无谓拖慢 2 秒）。
        """
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
        while persist_queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            if worker is not None and not worker.is_alive():
                break
            time.sleep(_PERSIST_DRAIN_POLL_S)
        return persist_queue.unfinished_tasks == 0

    async def drain(self, timeout: int = _PERSIST_DRAIN_TIMEOUT_MS) -> bool:
        """功能：等待持久化队列排空（毫秒上界）——关闭/测试前置屏障（不阻塞事件循环）。

        返回 True = 队列已空；False = 超时（写线程卡住/磁盘慢），调用方可继续
        （close 仍有二次有界 join 兜底）。pending 计数归写线程维护，本方法只 flush。
        """
        persist_queue = self._persist_queue
        if not self._persist_enabled or persist_queue is None:
            # 不能让调用方把「已关闭」误读成「已全部持久化」
            if self._persist_dropped:
                logger.warning(
                    "drain() on disabled persistence: %d event(s) were dropped earlier",
                    self._persist_dropped,
                )
            return True
        drained = await asyncio.to_thread(
            self._drain_blocking, timeout, persist_queue, self._persist_thread
        )
        if drained and self._persist_file is not None and not self._persist_file.closed:
            # 排空即刷盘：drain 语义 = 持久化屏障（写线程已空闲，无并发写）
            try:
                self._persist_file.flush()
            except (ValueError, OSError) as e:
                # 🟢-4：与并发 disable 关文件的窗口竞态——flush 失败不得炸调用方
                # （close 侧有最终 flush/close 兜底；此处仅屏障尽力而为）
                logger.debug("EventBus drain flush skipped: %s", e)
        return drained

    def _persist_worker(
        self, persist_queue: queue.Queue[dict[str, Any] | None], generation: int
    ) -> None:
        """功能：持久化写线程主循环——只消费启动时绑定的队列；None 哨兵即退出。

        队列满由 `emit` 侧丢弃计数；写异常吞掉记日志——写线程不得因单条失败退出。
        generation 为启动时代数——旧代记录在落盘前被校验丢弃（防跨代并发写）。
        """
        while True:
            record = persist_queue.get()
            try:
                if record is None:
                    return
                self._persist_one(record, generation)
            except Exception as e:  # noqa: BLE001 - 单条写失败不得杀死写线程
                logger.error("EventBus persist failed: %s", e)
            finally:
                persist_queue.task_done()

    def _persist_one(self, record: dict[str, Any], generation: int | None = None) -> None:
        """功能：单条事件落盘（写线程内执行；去重/旋转/按批 flush/关键事件 fsync）。

        generation 与当前代不一致时直接丢弃——旧代写线程不得触碰新代文件/哈希链。
        """
        if generation is not None:
            # 代数校验与文件句柄捕获同临界区：enable 换文件后旧代不可能捕获到新句柄
            with self._persist_state_lock:
                if generation != self._persist_generation:
                    return
                persist_file = self._persist_file
        else:
            persist_file = self._persist_file
        if persist_file is None or persist_file.closed:
            return
        event_id = str(record.get("event_id") or "")
        # 落盘去重（防跨 emit 恢复后的重放）——写线程单点判定，无并发
        if event_id and event_id in self._persisted_event_ids:
            return
        from bandi.observability.secret_mask import mask_dict

        # 🟢-8：非 dict data（list/tuple 等）直接落快照——mask_dict 只对 dict 生效
        # 不得因 'list' 无 .items() 让整条事件持久化失败
        data_snapshot = record.get("data")
        if data_snapshot is None:
            data_snapshot = {}
        masked_data = mask_dict(data_snapshot) if isinstance(data_snapshot, dict) else data_snapshot
        line = (
            json.dumps(
                {
                    "event_id": event_id,
                    "sequence": record.get("sequence", 0),
                    "type": record.get("type_name", ""),
                    "data": masked_data,
                    "timestamp": record.get("timestamp", 0.0),
                    "prev_hash": self._last_hash,
                    **{k: record.get(k, "") for k in _SOURCING_FIELDS},
                },
                ensure_ascii=False,
                # 不可序列化对象降级为字符串，避免 TypeError 逸出
                default=str,
            )
            + "\n"
        )
        # 的跨代写（旧代 worker 旋转/写新代文件、污染新代哈希链与计数）
        with self._persist_state_lock:
            if generation is not None and generation != self._persist_generation:
                return
            if self._written_bytes > _PERSIST_ROTATE_BYTES and event_id:
                self._rotate()
                persist_file = self._persist_file
                if persist_file is None or persist_file.closed:
                    return
            try:
                _lock_byte(persist_file)
                persist_file.write(line)
            finally:
                _unlock_byte(persist_file)
            self._written_bytes += len(line.encode("utf-8"))
            self._last_hash = hashlib.sha256(line.rstrip("\n").encode("utf-8")).hexdigest()
            if event_id:
                self._persisted_event_ids.add(event_id)
                self._persisted_order.append(event_id)
            self._persist_pending += 1
            event_type = record.get("type")
            if self._persist_pending >= _PERSIST_FLUSH_BATCH or event_type in _FORCE_FLUSH_TYPES:
                persist_file.flush()
                if event_type in _FORCE_FLUSH_TYPES:
                    import os as _os

                    _os.fsync(persist_file.fileno())
                self._persist_pending = 0

    def replay(self, dry_run: bool = True) -> EventReplayReport:
        """功能：只读重放审计事件日志——校验 hash 链/sequence 单调/event_id 唯一（含损坏行定位）。

        审计规则（dry_run=True 纯读，不写不旋转不重置状态）：
        - 哈希链：第 i 行 prev_hash 必须等于第 i-1 行原文 sha256（首行为空）。
        - sequence：必须严格单调递增。
        - event_id：非空且全局唯一。
        - 损坏行：非法 JSON 记为 parse 错误，跳过但不阻断后续行。
        dry_run=False 时额外将最后完好行状态恢复到 bus（sequence/hash/去重集）
        语义与 enable_persistence 的恢复一致，但先做整文件校验再落状态。
        """
        report = EventReplayReport()
        if self._persist_path is None or not self._persist_path.exists():
            report._record(0, "missing", "persist file not found")
            return report

        lines = self._persist_path.read_text(encoding="utf-8").splitlines()
        report.lines_read = len(lines)
        prev_hash = ""
        prev_seq = 0
        seen_ids: set[str] = set()
        last_good_line = ""
        last_seq = 0
        first_set = False

        for idx, raw_line in enumerate(lines):
            line_no = idx + 1
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line)
            except (ValueError, TypeError, json.JSONDecodeError):
                report._record(line_no, "parse", "invalid JSON line")
                continue

            # 字段级防御（fail-closed）：畸形但可解析的字段记 field 错误而非崩溃。
            # 非法 JSON 已在上方拦；此处防 event_id 非 str / sequence 非 int 等注入。
            event_id = obj.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                report._record(line_no, "field", "event_id must be a non-empty string")
                event_id = ""
            raw_seq = obj.get("sequence", 0)
            try:
                if isinstance(raw_seq, bool):
                    raise ValueError("sequence cannot be bool")
                seq = int(raw_seq or 0)
            except (TypeError, ValueError):
                seq = -1
                report._record(line_no, "field", "sequence must be an int")

            field_ok = bool(event_id) and seq >= 0
            # 哈希链：对任意可读行（含畸形/无效行）都用原文推进，保证后续行
            # 的 prev_hash 校验不因前一行异常而级联误报（与 emit _last_hash 语义一致）。
            chain_hash = obj.get("prev_hash")
            if chain_hash != prev_hash:
                if line_no == 1 and chain_hash:
                    # 旋转续接：新代文件首行 prev_hash 指向旧链尾（跨代链设计
                    report.continues_from_previous = True
                else:
                    report.hash_chain_broken = True
                    report._record(line_no, "hash_chain", f"prev_hash mismatch (line {line_no})")
            prev_hash = hashlib.sha256(raw_line.rstrip("\n").encode("utf-8")).hexdigest()

            if not field_ok:
                continue
            if event_id in seen_ids:
                report._record(line_no, "duplicate", f"event_id={event_id}")
                if event_id not in report.duplicate_event_ids:
                    report.duplicate_event_ids.append(event_id)
            seen_ids.add(event_id)
            if seq <= prev_seq:
                report._record(line_no, "sequence", f"sequence not monotonic: {seq} <= {prev_seq}")
            prev_seq = seq
            last_seq = seq
            report.valid_lines += 1
            if not first_set:
                report.first_sequence = seq
                first_set = True
            last_good_line = raw_line

        report.last_sequence = last_seq
        if len(report.duplicate_event_ids) > 0:
            report.ok = False

        if not dry_run:
            # 恢复模式：把最后完好行状态落到 bus（与 enable_persistence 恢复语义一致）
            self._sequence = max(self._sequence, last_seq)
            if last_good_line:
                self._last_hash = hashlib.sha256(
                    last_good_line.rstrip("\n").encode("utf-8")
                ).hexdigest()
        return report

    def _rotate(self) -> None:
        # 功能：七-旋转——B5c 8.11：多代滚动（.1→.2→.3，最多保留
        # _PERSIST_ROTATE_KEEP 代）；新写进入新文件
        self._persist_file.flush()
        self._persist_file.close()
        # 旧代顺移：.2→.3、.1→.2（超保留代数的最老代删除）
        for i in range(_PERSIST_ROTATE_KEEP - 1, 0, -1):
            src = self._persist_path.with_suffix(self._persist_path.suffix + f".{i}")
            dst = self._persist_path.with_suffix(self._persist_path.suffix + f".{i + 1}")
            if not src.exists():
                continue
            dst.unlink(missing_ok=True)
            try:
                src.rename(dst)
            except OSError:
                pass
        rolled = self._persist_path.with_suffix(self._persist_path.suffix + ".1")
        rolled.unlink(missing_ok=True)
        try:
            self._persist_path.rename(rolled)
        except OSError as exc:
            # 原文件继续写（超限优先于丢事件）；_written_bytes 保持超限，下次再试
            logger.warning("persist rotation failed, continuing in current file: %s", exc)
            try:
                self._persist_file = self._persist_path.open("a", encoding="utf-8")
            except OSError as reopen_exc:
                logger.error("persist reopen failed after rotation failure: %s", reopen_exc)
            return
        self._persist_file = self._persist_path.open("a", encoding="utf-8")
        # B5c 8.11：persisted 去重集不再清空（旋转后同 id 重复 emit 不得重复落盘）
        # 可能丢最新 id 致可重复落盘）
        while len(self._persisted_event_ids) > _MAX_SEEN_EVENT_IDS and self._persisted_order:
            self._persisted_event_ids.discard(self._persisted_order.popleft())
        self._written_bytes = 0

    async def subscribe(  # 功能：注册指定类型的事件监听器
        self, event_type: EventType, handler: Callable[[Event], Coroutine[Any, Any, None]]
    ) -> None:
        async with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    async def unsubscribe(  # 功能：移除指定类型的监听器
        self, event_type: EventType, handler: Callable[[Event], Coroutine[Any, Any, None]]
    ) -> None:
        async with self._lock:
            if event_type in self._handlers:
                self._handlers[event_type] = [
                    h for h in self._handlers[event_type] if h is not handler
                ]

    def _fill_sourcing_fields(self, event: Event) -> None:
        ctx = _event_ctx_var.get()
        if ctx is None:
            return
        for k, v in ctx.as_dict().items():
            if not getattr(event, k):
                setattr(event, k, v)

    @staticmethod
    def _snapshot_event_data(data: Any) -> Any:
        """功能：入队前对事件 data 做稳定快照——emit 后改写原对象不污染落盘内容。

        dict/list/tuple 等一律 deepcopy（嵌套一起冻结）；不可深拷贝的 dict 回落
        浅拷贝 + 逐值复制；其余不可深拷贝对象回落原引用（不做结构假设）。
        """
        try:
            return copy.deepcopy(data)
        except Exception:  # noqa: BLE001 — 含不可深拷贝对象时退级
            if not isinstance(data, dict):
                return data
            snapshot = dict(data)
            for key, value in data.items():
                try:
                    snapshot[key] = copy.copy(value)
                except Exception:  # noqa: BLE001 — 单值不可复制则保留引用
                    pass
            return snapshot

    async def emit(self, event: Event) -> None:  # 功能：发布事件到总线，并行通知所有监听器
        async with self._emit_lock:
            if not event.event_id:
                event.event_id = uuid4().hex
            if event.sequence == 0:
                self._sequence += 1
                event.sequence = self._sequence
            self._fill_sourcing_fields(event)
        # 不重复通知任何订阅者（MetricsBridge/OTel/未来订阅者自动幂等）；
        # B5c 8.11：超限滑动逐出最旧（原 clear 全清致近期幂等断层）
        if event.event_id in self._seen_event_ids:
            return
        self._seen_event_ids.add(event.event_id)
        self._seen_order.append(event.event_id)
        while len(self._seen_order) > _MAX_SEEN_EVENT_IDS:
            oldest = self._seen_order.popleft()
            self._seen_event_ids.discard(oldest)
        if self._persist_enabled and self._persist_queue is not None:
            record = {
                "event_id": event.event_id,
                "sequence": event.sequence,
                "type_name": event.type.name,
                "type": event.type,
                "data": self._snapshot_event_data(event.data),
                "timestamp": event.timestamp,
                **{k: getattr(event, k) for k in _SOURCING_FIELDS},
            }
            try:
                self._persist_queue.put_nowait(record)
            except queue.Full:
                self._account_dropped(1)
                logger.warning(
                    "EventBus persist queue full; event dropped (event_id=%s dropped=%d)",
                    event.event_id,
                    self._persist_dropped,
                )
        async with self._lock:
            handlers = list(self._handlers.get(event.type, []))
        if not handlers:
            return
        results = await asyncio.gather(*[h(event) for h in handlers], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                self._handler_error_count += 1
                logger.error(f"EventBus handler failed for {event.type}: {r}")
                if self._fail_fast_on_handler_error:
                    raise r

    async def close(self) -> None:
        self.disable_persistence()


async def bootstrap_event_bus(
    bus: EventBus, deps: Dependencies
) -> None:  # 功能：初始化 EventBus，绑定横切关注点监听器
    missing: list[str] = []
    for handler_name, obj, event_type in [
        ("on_llm_response", deps.cost_tracker, EventType.LLM_RESPONSE),
        ("on_tool_done", deps.cost_tracker, EventType.TOOL_COMPLETED),
        ("on_state_change", deps.session_store, EventType.STATE_CHANGED),
        ("on_terminated", deps.session_store, EventType.TERMINATED),
        ("on_error", deps.logger, EventType.ERROR_OCCURRED),
        ("on_degradation", deps.logger, EventType.DEGRADATION),
    ]:
        if obj is None:
            # 功能：缺依赖必须**显式可见**——原实现静默 `continue`
            # 造成「代码看着接线了、实际订阅未建立」的伪接线：EventBus 照常收事件
            # 但计费/落库/日志侧永远不响应，排障时无任何线索。
            missing.append(f"{getattr(event_type, 'name', event_type)}←{handler_name}")
            continue
        handler = getattr(obj, handler_name, None)
        if handler is not None:
            await bus.subscribe(event_type, handler)
    if missing:
        logger.warning(
            "bootstrap_event_bus：%d 个横切订阅因依赖缺失未建立（伪接线风险）：%s",
            len(missing),
            ", ".join(missing),
        )

    if deps.trace_explainer is not None:
        await deps.trace_explainer.start()

    from bandi.observability.bridge import MetricsBridge

    await MetricsBridge().wire(bus)

    from bandi.observability.tracer import OTelEventBusBridge, get_shared_tracer

    # TracerProvider）——事件 span 自动嵌套在代码 span 下（OTel 上下文传播）
    # 消除双体系各建 provider 的割裂
    bridge = OTelEventBusBridge(tracer=get_shared_tracer())
    for et in (
        EventType.LLM_REQUEST,
        EventType.LLM_RESPONSE,
        EventType.LLM_ERROR,
        EventType.TOOL_INVOKED,
        EventType.TOOL_COMPLETED,
        EventType.TOOL_ERROR,
        EventType.STATE_CHANGED,
        EventType.TERMINATED,
    ):
        await bus.subscribe(et, bridge.handle_event)
