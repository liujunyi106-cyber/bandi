from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import bandi.agent.factory as factory_module
from bandi.agent.checkpoint import Checkpoint, CheckpointStore
from bandi.agent.factory import _resolve_account_store, build_agent_context, build_policy_engine
from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.gate_harness import GateHarness, GateVerdict
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.recruiter import Recruiter
from bandi.company.seat_executor import resolve_requester_user_id
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.core.account import DEFAULT_LOCAL_USER_ID
from bandi.core.config import BandiConfig
from bandi.core.event_bus import EventBus, reset_event_context, set_event_context
from bandi.core.events import Event, EventType
from bandi.core.message import ToolCall, ToolResult
from bandi.tools.base import BaseTool, ToolPermissionContext
from bandi.tools.executor import ToolExecutor
from bandi.tools.memory_tools import MemorySearchTool
from bandi.tools.middleware.circuit_breaker_mw import CircuitBreakerMiddleware, CircuitState
from bandi.tools.registry import ToolRegistry

# 功能：契约——GlobalMemoryStore.append_event 的 event_id 幂等原子性（同族 TOCTOU）
#
# 单实例内 `self._lock` 掩盖了窗口；**跨 store 实例（生产每 turn 重建管理器）
# 或跨进程**时，两个连接可在彼此 commit 前都读到「无此 event_id」，各自插入
# → 同 event_id 重复入事件表（幂等承诺跨连接失效）。
#
# 修法对齐同文件 `put_global_memory_cas`：`BEGIN IMMEDIATE` 把查重 + 插入 +
#
# 本契约钉行为（不钉实现）：
# 1. 两 store 实例同库并发 append 同 event_id → 事件表恰一条、双方拿到同一 seq；
# 2. 并发 append 不同 event_id → 全入且序号连续不重（原子化不得吞写/跳号）；


_CONCURRENT_ROUNDS = 50
_PER_THREAD = 50


def test_append_event_same_id_idempotent_single_and_cross_instance(tmp_path: Path) -> None:
    """破坏者视角：两 store 实例同库并发 append 同 event_id → 只入一条。

    修前：SELECT→INSERT 窗口内双方都读不到既有 id，各自 INSERT（重复事件）。
    回归：单实例幂等语义不变（重复 event_id 返回既有 seq，payload 不覆盖）。
    """
    from bandi.memory.global_store import GlobalMemoryStore

    single = GlobalMemoryStore(tmp_path / "single.db")
    try:
        seq1 = single.append_event("E", {"n": 1}, event_id="same")
        seq2 = single.append_event("E", {"n": 2}, event_id="same")
        assert seq1 == seq2
        events = single.list_events()
        assert len(events) == 1
        assert events[0]["payload"]["n"] == 1, "幂等命中不得覆盖原 payload"
    finally:
        single.close()

    stores = [GlobalMemoryStore(tmp_path / "g.db"), GlobalMemoryStore(tmp_path / "g.db")]
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for round_no in range(_CONCURRENT_ROUNDS):
            event_id = f"evt-{round_no}"
            barrier = threading.Barrier(2)
            results: dict[int, int] = {}
            errors: list[Exception] = []

            def _append(index: int) -> None:
                try:
                    barrier.wait()
                    results[index] = stores[index].append_event(
                        "E", {"i": round_no}, event_id=event_id
                    )
                except Exception as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
                    errors.append(exc)

            threads = [threading.Thread(target=_append, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            assert not errors, f"round {round_no} 并发 append 抛异常：{errors}"
            assert results[0] == results[1], (
                f"round {round_no}：同 event_id 幂等必须返回同一 seq，实得 {results}"
            )
            events = stores[0].list_events(limit=_CONCURRENT_ROUNDS + 10)
            assert len(events) == round_no + 1, (
                f"round {round_no}：同 event_id 只应入一条，事件表实得 {len(events)} 条"
            )
    finally:
        sys.setswitchinterval(previous)
        for store in stores:
            store.close()


def test_concurrent_distinct_event_ids_all_kept_and_monotonic(tmp_path: Path) -> None:
    """并发不同 event_id：全部入账、序号唯一且连续（原子化不得吞写/跳号）。"""
    from bandi.memory.global_store import GlobalMemoryStore

    stores = [GlobalMemoryStore(tmp_path / "g.db"), GlobalMemoryStore(tmp_path / "g.db")]
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _append(index: int, tag: str) -> None:
        try:
            barrier.wait()
            for i in range(_PER_THREAD):
                stores[index].append_event("E", {"tag": tag, "i": i}, event_id=f"{tag}-{i}")
        except Exception as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
            errors.append(exc)

    threads = [
        threading.Thread(target=_append, args=(0, "A")),
        threading.Thread(target=_append, args=(1, "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    total = 2 * _PER_THREAD
    try:
        assert not errors, f"并发 append 异常：{errors}"
        events = stores[0].list_events(limit=total + 10)
        assert len(events) == total, f"并发不同 id 必须全入，实得 {len(events)}/{total}"
        seqs = sorted(event["seq"] for event in events)
        assert len(set(seqs)) == total, "序号不得重复"
        assert seqs == list(range(1, total + 1)), (
            f"序号必须在并发下保持连续（不吞写/跳号），实得 {seqs[:5]}...{seqs[-5:]}"
        )
        raw = stores[0]._conn.execute("SELECT COUNT(*) FROM event_sequence").fetchone()[0]
        assert int(raw) == total, "event_sequence 登记数必须与事件数一致"
    finally:
        for store in stores:
            store.close()


# ==================== c1_event_sourcing ====================


def test_event_sourcing_fields_and_emit_context_fill(tmp_path: Path) -> None:
    ev = Event(type=EventType.TURN_STARTED, data={})
    for field in (
        "trace_id",
        "session_id",
        "turn_id",
        "attempt_id",
        "parent_event_id",
        "schema_version",
        "source",
        "command_id",
    ):
        assert hasattr(ev, field), f"Event 缺 {field}"
    assert ev.schema_version > 0
    ev.session_id = "s1"
    assert ev.session_id == "s1"

    bus = EventBus(tmp_path / "events.jsonl")
    bus.enable_persistence()

    token = set_event_context(trace_id="tr-1", session_id="s-ctx", turn_id="t3", source="contract")

    async def run() -> None:
        await bus.emit(Event(type=EventType.LLM_REQUEST, data={}))
        await bus.close()

    asyncio.run(run())
    # 污染后续 emit 的 session_id）——必须 reset 恢复
    reset_event_context(token)
    rows = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    row = rows[0]
    assert row.get("session_id") == "s-ctx"
    assert row.get("turn_id") == "t3"
    assert row.get("trace_id") == "tr-1"
    assert row.get("source") == "contract"


def test_persistence_dedup_terminal_flush_and_fsync(tmp_path: Path, monkeypatch: Any) -> None:
    """同 event_id 只落盘一次；TERMINATED 强制 flush 且仅关键事件触发 fsync。"""
    import os

    bus = EventBus(tmp_path / "events.jsonl")
    bus.enable_persistence()
    ev = Event(type=EventType.TOOL_INVOKED, data={"tool": "bash"}, event_id="dup-1")

    async def run() -> None:
        await bus.emit(ev)
        await bus.emit(ev)
        await bus.close()

    asyncio.run(run())
    rows = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1, f"同 event_id 只落盘一次（实际 {len(rows)}）"

    fsync_calls: list[int] = []
    orig_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: fsync_calls.append(fd) or orig_fsync(fd))
    bus2 = EventBus(tmp_path / "events2.jsonl")
    bus2.enable_persistence()

    async def run2() -> None:
        await bus2.emit(Event(type=EventType.TOOL_INVOKED, data={}))
        await bus2.emit(Event(type=EventType.TERMINATED, data={}))
        await bus2.close()

    asyncio.run(run2())
    rows2 = (tmp_path / "events2.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows2) == 2, "TERMINATED 必须强制 flush（关闭前已落盘）"
    assert len(fsync_calls) == 1, f"仅关键事件触发 fsync（实际 {len(fsync_calls)}）"


def test_handler_error_fail_fast_and_record_only_modes(tmp_path: Path, caplog: Any) -> None:
    import logging

    bus = EventBus(tmp_path / "events.jsonl", fail_fast_on_handler_error=True)

    async def boom(ev: Event) -> None:
        raise RuntimeError("handler boom")

    async def run() -> None:
        await bus.subscribe(EventType.ERROR_OCCURRED, boom)
        try:
            await bus.emit(Event(type=EventType.ERROR_OCCURRED, data={}))
        except RuntimeError as e:
            assert "handler boom" in str(e)
            return
        raise AssertionError("fail-fast 模式必须向上抛 handler 异常")

    asyncio.run(run())

    bus2 = EventBus(tmp_path / "events2.jsonl")

    async def run2() -> None:
        await bus2.subscribe(EventType.ERROR_OCCURRED, boom)
        await bus2.emit(Event(type=EventType.ERROR_OCCURRED, data={}))

    with caplog.at_level(logging.ERROR):
        asyncio.run(run2())
    assert any("handler boom" in r.message for r in caplog.records)
    assert bus2.handler_error_count >= 1


# ==================== c1_global_save_merge ====================
# 功能：契约——全局共享层 save 语义归一（并发工程复核 🔴-2）
#
# `memory_manager.save_memory(SCOPE_GLOBAL, allow_shared=True)` 走
# `put_global_memory` = `INSERT OR REPLACE` 整键替换，且无锁无 CAS；
# 同一单键上 `append_memory` 已有锁 + CAS + 合并 → 两路语义互斥。
# 本契约钉：全局共享层写入 = **合并**（不覆盖）/ 重放幂等 / 与 append 语义归一 /
# 并发写不丢更新 / 未显式共享的写入行为不变。


def r2xc1_hierarchy(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


class TestGlobalSaveMerges:
    """🔴-2：全局共享层 save 必须合并，不得整键覆盖。"""

    def test_save_merges_across_writes_and_managers(self, tmp_path: Any) -> None:
        """**破坏者视角**：连续写两条教训，第一条不得消失（原实现只剩最后一条）；
        两个 agent 各自的 manager 指向同一库——后写者不得抹掉前者（丢数据回归）。"""
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "[教训] A：超时后不要重试旧 id", allow_shared=True)
        h.save_memory(SCOPE_GLOBAL, "[教训] B：写文件前先确认父目录", allow_shared=True)

        text = h.load_memory(SCOPE_GLOBAL)
        assert "教训] A" in text, f"第一条教训被覆盖丢失：{text!r}"
        assert "教训] B" in text, f"第二条教训未写入：{text!r}"

        agent_a = r2xc1_hierarchy(tmp_path)
        agent_b = r2xc1_hierarchy(tmp_path)

        agent_a.save_memory(SCOPE_GLOBAL, "[教训] A 的结论", allow_shared=True)
        agent_b.save_memory(SCOPE_GLOBAL, "[教训] B 的结论", allow_shared=True)

        text2 = agent_a.load_memory(SCOPE_GLOBAL)
        assert "A 的结论" in text2 and "B 的结论" in text2, f"跨 agent 覆盖：{text2!r}"

    def test_global_write_replay_is_idempotent_for_save_and_append(self, tmp_path: Any) -> None:
        """同 turn 重放（checkpoint 恢复）重复写入同一教训不得堆叠；
        append 与 SESSION/EMPLOYEE 分支同口径——同 entry 重放不叠加。"""
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        lesson = "[教训] 任务：X\n结果：失败\n失败工具：bash"
        h.save_memory(SCOPE_GLOBAL, lesson, allow_shared=True)
        h.save_memory(SCOPE_GLOBAL, lesson, allow_shared=True)

        assert h.load_memory(SCOPE_GLOBAL).count(lesson) == 1

        h.append_memory(SCOPE_GLOBAL, "同一条教训", allow_shared=True)
        h.append_memory(SCOPE_GLOBAL, "同一条教训", allow_shared=True)

        assert h.load_memory(SCOPE_GLOBAL).count("同一条教训") == 1

    def test_save_and_append_coherent_and_append_accumulates(self, tmp_path: Any) -> None:
        """语义归一：save 与 append 对同一单键必须互相可见（原实现互斥）；
        回归：append 的「不同条目累积」语义不得被本次修复破坏。"""
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "save 写入", allow_shared=True)
        h.append_memory(SCOPE_GLOBAL, "append 写入", allow_shared=True)

        text = h.load_memory(SCOPE_GLOBAL)
        assert "save 写入" in text and "append 写入" in text, f"两路互斥：{text!r}"

        h.append_memory(SCOPE_GLOBAL, "条目一", allow_shared=True)
        h.append_memory(SCOPE_GLOBAL, "条目二", allow_shared=True)

        text2 = h.load_memory(SCOPE_GLOBAL)
        assert "条目一" in text2 and "条目二" in text2

    def test_save_capacity_keeps_newest(self, tmp_path: Any) -> None:
        """容量约束：超限时必须保留最新内容（不得丢新写入）。"""
        from bandi.memory.global_store import GLOBAL_MEMORY_MAX_CHARS
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "旧" * (GLOBAL_MEMORY_MAX_CHARS - 10), allow_shared=True)
        h.save_memory(SCOPE_GLOBAL, "★最新教训★", allow_shared=True)

        text = h.load_memory(SCOPE_GLOBAL)
        assert "★最新教训★" in text, "超容量时新写入被丢弃"
        assert len(text) <= GLOBAL_MEMORY_MAX_CHARS


class TestGlobalCapacityKeepsNewestIntact:
    """🔴-A：超容量合并必须保尾（最新）且不产生截断碎片。

    旧实现两处截断方向相反：`_merge_global_shared` 按原始字符数保尾裁剪，但
    `json.dumps` 把每个换行转义成 2 字符后仍超限 → `_prepare_global_value`
    保头二次截断 → 最新条目尾巴被砍、结尾留下半个条目的碎片。
    """

    def test_200_newline_entries_keep_latest_without_fragments(self, tmp_path: Any) -> None:
        import re

        from bandi.memory.global_store import GLOBAL_MEMORY_MAX_CHARS
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        for i in range(200):
            h.save_memory(
                SCOPE_GLOBAL, f"[E{i:03d}]\n教训内容-{i:03d}\n处置：重试前先检查", allow_shared=True
            )

        text = h.load_memory(SCOPE_GLOBAL)
        assert "[E199]\n教训内容-199" in text, f"最新教训必须完整可见：{text[-200:]!r}"
        assert len(text) <= GLOBAL_MEMORY_MAX_CHARS
        complete = re.findall(r"\[E(\d{3})\]\n教训内容-\1", text)
        assert len(complete) == text.count("[E"), (
            f"存在截断碎片（{text.count('[E') - len(complete)} 处半个条目）：{text[:200]!r}"
        )

    def test_save_global_blank_content_is_noop(self, tmp_path: Any) -> None:
        """🟢-G：空/纯空白内容不得写入（原实现落成孤零零的分隔换行）。"""
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "", allow_shared=True)
        h.save_memory(SCOPE_GLOBAL, "   ", allow_shared=True)

        assert h.load_memory(SCOPE_GLOBAL) == ""


class TestGlobalSaveGuardsUnchanged:
    """既有护栏回归：未显式共享的写入行为不得因本次修复改变。"""

    def test_save_guards_reject_without_ids_and_downgrade_with_employee(
        self, tmp_path: Any
    ) -> None:
        from bandi.memory.memory_manager import SCOPE_EMPLOYEE, SCOPE_GLOBAL

        with pytest.raises(ValueError):
            r2xc1_hierarchy(tmp_path).save_memory(SCOPE_GLOBAL, "未声明共享")

        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "员工私有内容", employee_id="emp-1")

        assert h.load_memory(SCOPE_EMPLOYEE, employee_id="emp-1")
        assert "员工私有内容" not in (h.load_memory(SCOPE_GLOBAL) or "")


class TestGlobalTrimHighEscape:
    """🟡-1（复审）：高转义文本下裁剪不得清空整块全局记忆。

    旧实现把「JSON 编码长度差」直接当原始字符偏移：引号/反斜杠转义开销超过
    容量时偏移大于全文长度 → `text[cut]` 为空（新旧全丢）。修复：二分定位
    满足编码上限的最小原始索引。
    """

    @pytest.mark.parametrize(
        "payload",
        ['"' * 5000, "\\" * 5000, "\U0001f600" * 5000, '"' * 5000 + "\n"],
        ids=["quotes", "backslashes", "emoji", "trailing-newline"],
    )
    def test_high_escape_write_never_wipes_memory(self, tmp_path: Any, payload: str) -> None:
        from bandi.memory.global_store import GLOBAL_MEMORY_MAX_CHARS
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "[教训] 旧内容先写入", allow_shared=True)
        h.save_memory(SCOPE_GLOBAL, payload, allow_shared=True)

        text = h.load_memory(SCOPE_GLOBAL)
        assert text, "高转义写入把整块全局记忆裁成空（新旧全丢回归）"
        assert len(json.dumps(text, ensure_ascii=False)) <= GLOBAL_MEMORY_MAX_CHARS
        assert text.endswith(payload[-10:]), "最新写入必须保尾（不得丢新保旧）"

    def test_high_escape_entry_stays_complete_when_within_capacity(self, tmp_path: Any) -> None:
        """容量内的最新高转义条目必须完整保留（裁剪只裁旧内容）。"""
        from bandi.memory.global_store import GLOBAL_MEMORY_MAX_CHARS
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        entry = '"' * 1500
        h = r2xc1_hierarchy(tmp_path)
        h.save_memory(SCOPE_GLOBAL, "旧" * 3000, allow_shared=True)
        h.save_memory(SCOPE_GLOBAL, entry, allow_shared=True)

        text = h.load_memory(SCOPE_GLOBAL)
        assert entry in text, "容量内的最新高转义条目被截断"
        assert len(json.dumps(text, ensure_ascii=False)) <= GLOBAL_MEMORY_MAX_CHARS


class TestGlobalTrimKeepsWholeEntries:
    """🟡-2（复审）：行边界裁剪后不得残留「半截条目」。

    多行条目按换行边界裁剪后，保留头部可能从条目的下半截开始（无条目标记的
    残行）。修复：对齐行边界后继续跳过残行，直到完整条目头（生产教训条目以
    `[` 开头；未知格式回退编码边界，方向保守）。
    """

    def test_single_line_trim_returns_text_at_entry_boundary(self) -> None:
        """保留头恰好落在条目头时，裁剪函数必须返回文本而不是 None。"""
        from bandi.memory.memory_manager import _trim_global_merged

        text = "\n".join([f"[教训|x] {'a' * 100}" for _ in range(60)])
        trimmed = _trim_global_merged(text)
        assert trimmed is not None, "裁剪返回 None，全局层会被写成 JSON null"
        assert trimmed.startswith("["), f"保留头不是完整条目：{trimmed[:20]!r}"

    def test_single_line_entries_never_clear_global_layer(self, tmp_path: Any) -> None:
        """单行教训条目溢出时，保留头恰好落在条目头，不得返回 None 清空全局层。"""
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        for i in range(60):
            h.save_memory(
                SCOPE_GLOBAL,
                f"[教训|team=t{i:02d}] " + "长" * 100,
                allow_shared=True,
            )

        text = h.load_memory(SCOPE_GLOBAL)
        assert text, "单行条目溢出后全局记忆被清空"
        assert text.startswith("["), f"保留头不是完整条目标记：{text[:80]!r}"
        assert "[教训|team=t59]" in text, "最新条目必须完整保留"

    @pytest.mark.parametrize("width", [3, 20])
    def test_200_two_line_entries_have_whole_head_entry(self, tmp_path: Any, width: int) -> None:
        from bandi.memory.global_store import GLOBAL_MEMORY_MAX_CHARS
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        h = r2xc1_hierarchy(tmp_path)
        for i in range(200):
            h.save_memory(
                SCOPE_GLOBAL, f"[E{i:03d}]\n教训内容-{i:03d}-" + "长" * width, allow_shared=True
            )

        text = h.load_memory(SCOPE_GLOBAL)
        assert "[E000]" not in text, "前置：必须发生裁剪，否则契约未覆盖"
        first_line = text.split("\n", 1)[0]
        assert re.fullmatch(r"\[E\d{3}\]", first_line), f"保留头不是完整条目头：{first_line!r}"
        complete = re.findall(r"\[E(\d{3})\]\n教训内容-\1-长+", text)
        assert len(complete) == text.count("[E"), (
            f"存在半截条目（{text.count('[E') - len(complete)} 处）：{text[:120]!r}"
        )
        newest = "[E199]\n教训内容-199-" + "长" * width
        assert newest in text, "最新条目必须完整保留"
        assert len(json.dumps(text, ensure_ascii=False)) <= GLOBAL_MEMORY_MAX_CHARS


# ==================== c1_memory_isolation ====================
# 功能：契约——全局记忆隔离 + 并发写 CAS（并发工程 批次3 / ）
#
# `memory_manager.py` 单键共享全局层（所有 agent 同一份）；`append_memory`
# load→merge→save 无锁无 CAS（并发丢更新）；`long_term` 漏传维度退化为
# `IS NULL` 静默落全局层。本契约钉：全局写入显式化（allow_shared）/
# 版本 CAS 防丢更新 / 漏传维度不静默（strict 计数）/ 员工层隔离不变。


def r3xc1_hierarchy(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(tmp_path / "global.db"),
        markdown_memory=MarkdownMemoryStore(tmp_path / "store", project_root=tmp_path / "proj"),
        work_item_store=WorkItemStore(str(tmp_path / "company.db")),
        session_store=SessionStore(tmp_path / "sessions.db"),
    )


class TestGlobalWriteExplicit:
    """共享全局层写入必须显式 allow_shared，默认降级/拒绝（不静默写白板）。"""

    def test_global_write_without_allow_shared_and_ids_rejected(self, tmp_path: Any) -> None:
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        hierarchy = r3xc1_hierarchy(tmp_path)
        with pytest.raises(ValueError):
            hierarchy.save_memory(SCOPE_GLOBAL, "未声明共享的写入")
        with pytest.raises(ValueError):
            hierarchy.append_memory(SCOPE_GLOBAL, "未声明共享的追加")

    def test_allow_shared_visible_and_employee_fallback(self, tmp_path: Any) -> None:
        """兜底：有 employee_id 时降级写员工命名空间，不污染共享白板。"""
        from bandi.memory.memory_manager import SCOPE_EMPLOYEE, SCOPE_GLOBAL

        hierarchy = r3xc1_hierarchy(tmp_path)
        hierarchy.save_memory(SCOPE_GLOBAL, "共享事实", allow_shared=True)
        assert "共享事实" in hierarchy.load_memory(SCOPE_GLOBAL)

        hierarchy.save_memory(SCOPE_GLOBAL, "员工私有事实", project_id="p1", employee_id="e1")
        assert "员工私有事实" not in hierarchy.load_memory(SCOPE_GLOBAL), "不得写共享白板"
        assert "员工私有事实" in hierarchy.load_memory(
            SCOPE_EMPLOYEE, project_id="p1", employee_id="e1"
        )


class TestGlobalAppendLockedRetry:
    """增补：跨进程写锁竞争（OperationalError）时应重读版本再试，不直接炸主链。"""

    def test_append_retries_when_db_locked(self, tmp_path: Any, monkeypatch: Any) -> None:
        import sqlite3

        from bandi.memory.memory_manager import SCOPE_GLOBAL

        hierarchy = r3xc1_hierarchy(tmp_path)
        store = hierarchy._global
        original = store.put_global_memory_cas
        calls = {"n": 0}

        def flaky(key: str, content: str, expected_version: int) -> bool:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return original(key, content, expected_version)

        monkeypatch.setattr(store, "put_global_memory_cas", flaky)
        hierarchy.append_memory(SCOPE_GLOBAL, "锁竞争后重试成功", allow_shared=True)
        assert "锁竞争后重试成功" in hierarchy.load_memory(SCOPE_GLOBAL)
        assert calls["n"] == 2


class TestGlobalCas:
    """/d：全局层版本 CAS——陈旧版本不得覆盖（防并发丢更新）。"""

    def test_cas_rejects_stale_and_plain_put_bumps_version(self, tmp_path: Any) -> None:
        from bandi.memory.global_store import GlobalMemoryStore

        store = GlobalMemoryStore(tmp_path / "g.db")
        assert store.get_global_memory_version("k") == 0
        assert store.put_global_memory_cas("k", "v1", expected_version=0) is True
        assert store.get_global_memory_version("k") == 1

        assert store.put_global_memory_cas("k", "stale", expected_version=0) is False
        assert store.get("k") == "v1", "陈旧版本不得覆盖新值"

        assert store.put_global_memory_cas("k", "v2", expected_version=1) is True
        assert store.get("k") == "v2"

        store.put_global_memory("plain", "v1")
        assert store.get_global_memory_version("plain") == 1
        store.put_global_memory("plain", "v2")
        assert store.get_global_memory_version("plain") == 2

    def test_shared_append_keeps_both_entries(self, tmp_path: Any) -> None:
        from bandi.memory.memory_manager import SCOPE_GLOBAL

        hierarchy = r3xc1_hierarchy(tmp_path)
        hierarchy.append_memory(SCOPE_GLOBAL, "条目甲", allow_shared=True)
        hierarchy.append_memory(SCOPE_GLOBAL, "条目乙", allow_shared=True)

        text = hierarchy.load_memory(SCOPE_GLOBAL)
        assert "条目甲" in text and "条目乙" in text, "CAS 追加不得丢更新"


class TestScopeStrict:
    """漏传维度不再静默——strict 计数可见（读路径默认不计）。"""

    def test_scope_clause_strict_counts_and_read_path_silent(self) -> None:
        from bandi.memory.long_term import (
            LongTermMemory,
            reset_scope_strict_misses,
            scope_strict_misses,
        )

        reset_scope_strict_misses()
        LongTermMemory._scope_clause(None, None, strict=True)
        misses = scope_strict_misses()
        assert misses["project_id"] >= 1 and misses["employee_id"] >= 1

        reset_scope_strict_misses()
        LongTermMemory._scope_clause(None, None)  # 默认 strict=False
        assert all(v == 0 for v in scope_strict_misses().values())

    def test_store_write_without_dims_counted(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import (
            LongTermMemory,
            MemoryItem,
            reset_scope_strict_misses,
            scope_strict_misses,
        )

        memory = LongTermMemory(tmp_path / "lt.db")
        reset_scope_strict_misses()
        memory.store(MemoryItem(item_id="m1", content="无维度写入"))
        misses = scope_strict_misses()
        assert misses["project_id"] >= 1 or misses["employee_id"] >= 1


class TestScopeDimensionNormalization:
    """🟡-6：空串维度写读边界统一归一——空白字符串一律视同 None（全局层）。

    旧实现写入口按原值落库（""）、读入口按 `if project_id:` 换成 `IS NULL`
    写 "" 的行任何读路径都命中不了（空串与 NULL 语义互斥）。本契约钉：
    写 "" 读 None 命中 / 写 None 读 "" 命中 / 空白串同归一 / 真实维度不误伤 /
    历史 "" 行开库即归一（真实 tmp 库读回）。
    """

    def test_blank_and_none_dims_normalized_across_read_paths(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt1.db")
        memory.store(
            MemoryItem(item_id="m1", content="空串维度写入的内容"),
            project_id="",
            employee_id="",
        )

        assert [item.id for item in memory.retrieve("空串维度")] == ["m1"], (
            "写「空串」必须能按全局层（None）读回"
        )
        assert [item.id for item in memory.search_fts_ranked("空串维度")] == ["m1"]
        assert [item.id for item in memory.list_items()] == ["m1"]
        assert memory.scope_of("m1") == (None, None), "空串维度入库即应归一为全局层"
        memory.close()

        memory2 = LongTermMemory(tmp_path / "lt2.db")
        memory2.store(MemoryItem(item_id="m2", content="无维度写入的内容"))

        assert [item.id for item in memory2.retrieve("无维度", project_id="", employee_id="")] == [
            "m2"
        ], "读入参「空串」必须归一为全局层（命中写 None 的行）"
        assert [
            item.id for item in memory2.search_fts_ranked("无维度", project_id="", employee_id="")
        ] == ["m2"]
        assert [item.id for item in memory2.list_items(project_id="", employee_id="")] == ["m2"]
        memory2.close()

        memory3 = LongTermMemory(tmp_path / "lt3.db")
        memory3.store(
            MemoryItem(item_id="m3", content="空白维度写入的内容"),
            project_id="   ",
            employee_id="\t",
        )

        assert memory3.scope_of("m3") == (None, None), "空白串同归一"
        assert [item.id for item in memory3.retrieve("空白维度", project_id="  ")] == ["m3"]
        assert [item.id for item in memory3.list_items(project_id=" ", employee_id=" ")] == ["m3"]
        memory3.close()

    def test_real_dims_not_affected_by_normalization(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        memory.store(
            MemoryItem(item_id="m4", content="项目维度内容"), project_id="p1", employee_id="e1"
        )

        assert [
            item.id for item in memory.retrieve("项目维度", project_id="p1", employee_id="e1")
        ] == ["m4"]
        assert memory.retrieve("项目维度") == [], "归一不得让维度行泄漏进全局层"
        assert memory.retrieve("项目维度", project_id="p1") == [], "缺 employee 维仍应隔离"
        assert memory.list_scopes() == [("p1", "e1")]
        memory.close()

    def test_apply_batch_normalizes_dims_for_delete_validation(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        memory.store(MemoryItem(item_id="b1", content="批次删除目标"))

        memory.apply_batch(upserts=[], deletes=["b1"], project_id="", employee_id="")

        assert memory.get("b1") is None, "空串维度必须归一后作为全局层通过删除校验"
        memory.close()

    def test_legacy_empty_and_spaced_dims_migrated_on_open(self, tmp_path: Any) -> None:
        """旧版本落库的「空串」维度行必须开库归一后可读；旧库带内容但残留空格的
        维度（' p1 '）必须开库裁到 'p1'（原迁移只处理 TRIM(x)=''，实现维度空格孤儿）。"""
        from bandi.memory.long_term import LongTermMemory

        db = tmp_path / "lt.db"
        memory = LongTermMemory(db)
        memory._conn.execute(
            "INSERT INTO long_term_memory "
            "(id, content, metadata, timestamp, importance, project_id, employee_id) "
            "VALUES ('legacy', '历史空串行', '{}', 1.0, 0.5, '', '  ')"
        )
        memory._conn.execute(
            "INSERT INTO long_term_fts (id, content) VALUES ('legacy', '历史空串行')"
        )
        memory._conn.commit()
        memory.close()

        reopened = LongTermMemory(db)
        assert [item.id for item in reopened.list_items()] == ["legacy"], (
            "旧版本落库的「空串」维度行必须开库归一后可读"
        )
        assert reopened.scope_of("legacy") == (None, None)
        reopened.close()

        db2 = tmp_path / "lt2.db"
        memory2 = LongTermMemory(db2)
        memory2._conn.execute(
            "INSERT INTO long_term_memory "
            "(id, content, metadata, timestamp, importance, project_id, employee_id) "
            "VALUES ('spaced', '带空格维度旧行', '{}', 1.0, 0.5, ' p1 ', ' e1 ')"
        )
        memory2._conn.execute(
            "INSERT INTO long_term_fts (id, content) VALUES ('spaced', '带空格维度旧行')"
        )
        memory2._conn.commit()
        memory2.close()

        reopened2 = LongTermMemory(db2)
        try:
            assert [
                item.id
                for item in reopened2.retrieve("带空格维度", project_id="p1", employee_id="e1")
            ] == ["spaced"], "旧库 ' p1 ' 行迁移后必须按归一维度读回"
            assert [
                item.id for item in reopened2.list_items(project_id="p1", employee_id="e1")
            ] == ["spaced"], "list_scopes 显示的分区必须真实可列出（不得幻觉）"
            assert reopened2.retrieve("带空格维度") == [], "迁移不得把维度行泄漏进全局层"
        finally:
            reopened2.close()

        again = LongTermMemory(db2)
        try:
            assert [
                item.id for item in again.retrieve("带空格维度", project_id="p1", employee_id="e1")
            ] == ["spaced"], "迁移必须幂等（二次开库不得再改坏）"
        finally:
            again.close()


class TestRequireScope:
    """🟡-7：写入维度硬校验入口——require_scope=True 时双 None 拒绝（默认零行为）。

    判断依据：维护/整合晋升的候选情节可以是全局层（既有契约明确断言全局情节
    必须可晋升），故不强制接线；本参数供「必须带作用域」的调用方显式选用。
    既有 `_scope_clause(strict=True)` 保留为显式读语义（观测计数，见 契约）。
    """

    def test_require_scope_rejects_both_dims_missing_or_blank(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        with pytest.raises(ValueError, match="require_scope"):
            memory.store(MemoryItem(item_id="r1", content="无维度写入"), require_scope=True)
        assert memory.get("r1") is None, "拒绝写入不得落库"

        with pytest.raises(ValueError):
            memory.store(
                MemoryItem(item_id="r3", content="空白维度"),
                project_id="   ",
                employee_id="",
                require_scope=True,
            )
        assert memory.get("r3") is None
        memory.close()

    def test_require_scope_accepts_single_dim_and_default_keeps_global(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        memory.store(
            MemoryItem(item_id="r2", content="仅项目维度"), project_id="p1", require_scope=True
        )

        assert memory.scope_of("r2") == ("p1", None)
        assert [item.id for item in memory.retrieve("仅项目", project_id="p1")] == ["r2"]

        memory.store(MemoryItem(item_id="r4", content="默认全局写入"))

        assert memory.scope_of("r4") == (None, None)
        memory.close()


class TestEmployeeIsolation:
    """回归：员工层读写隔离（A 员工不得读 B 员工）。"""

    def test_employee_scope_isolation(self, tmp_path: Any) -> None:
        from bandi.memory.long_term import LongTermMemory, MemoryItem

        memory = LongTermMemory(tmp_path / "lt.db")
        memory.store(
            MemoryItem(item_id="m1", content="员工A的机密"), project_id="p", employee_id="A"
        )

        assert memory.retrieve("机密", project_id="p", employee_id="A"), "A 自己必须可读"
        assert memory.retrieve("机密", project_id="p", employee_id="B") == [], (
            "B 不得读到 A 的员工层记忆"
        )


class TestGlobalCasAtomicity:
    """缺陷 3：CAS 的 check+write 必须原子（BEGIN IMMEDIATE）——跨实例/进程不丢更新。"""

    def test_concurrent_same_version_single_winner_and_appends_keep_all(
        self, tmp_path: Any
    ) -> None:
        import sys
        import threading

        from bandi.memory.global_store import GlobalMemoryStore

        stores = [GlobalMemoryStore(tmp_path / "g.db"), GlobalMemoryStore(tmp_path / "g.db")]
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            for round_no in range(50):
                key = f"cas-{round_no}"
                barrier = threading.Barrier(2)
                results: dict[int, bool] = {}
                errors: list[Exception] = []

                def _cas(index: int) -> None:
                    try:
                        barrier.wait()
                        results[index] = stores[index].put_global_memory_cas(key, f"v{index}", 0)
                    except Exception as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
                        errors.append(exc)

                threads = [threading.Thread(target=_cas, args=(i,)) for i in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

                assert not errors, f"round {round_no} CAS 抛异常：{errors}"
                assert sum(1 for ok in results.values() if ok) == 1, (
                    f"round {round_no}：同一 expected_version 并发 CAS 恰应一人成功，实得 {results}"
                )

            stores2 = [
                GlobalMemoryStore(tmp_path / "g2.db"),
                GlobalMemoryStore(tmp_path / "g2.db"),
            ]
            key2 = "shared"
            per_thread = 50
            barrier2 = threading.Barrier(2)
            errors2: list[Exception] = []

            def _append(store: Any, tag: str) -> None:
                try:
                    barrier2.wait()
                    for i in range(per_thread):
                        entry = f"{tag}{i:03d}"
                        for _attempt in range(500):
                            expected = store.get_global_memory_version(key2)
                            current = store.get(key2) or ""
                            merged = f"{current}\n{entry}" if current else entry
                            if store.put_global_memory_cas(key2, merged, expected):
                                break
                        else:
                            errors2.append(RuntimeError(f"CAS retry exhausted for {entry}"))
                            return
                except Exception as exc:  # noqa: BLE001
                    errors2.append(exc)

            threads2 = [
                threading.Thread(target=_append, args=(stores2[0], "A")),
                threading.Thread(target=_append, args=(stores2[1], "B")),
            ]
            for thread in threads2:
                thread.start()
            for thread in threads2:
                thread.join()

            final = stores2[0].get(key2) or ""
            missing = [
                f"{t}{i:03d}" for t in "AB" for i in range(per_thread) if f"{t}{i:03d}" not in final
            ]
            assert not errors2, f"并发追加异常：{errors2}"
            assert not missing, f"并发追加丢更新，缺失 {len(missing)} 条：{missing[:10]}"
            assert stores2[0].get_global_memory_version(key2) == 2 * per_thread, (
                "每次成功 CAS 恰应递增一版——版本数少于成功写入数即丢更新/版本回退"
            )
        finally:
            sys.setswitchinterval(previous)

    def test_two_managers_concurrent_append_keep_all_entries(self, tmp_path: Any) -> None:
        import threading

        from bandi.memory.memory_manager import SCOPE_GLOBAL

        managers = [r3xc1_hierarchy(tmp_path), r3xc1_hierarchy(tmp_path)]
        per_thread = 50
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def _append(manager: Any, tag: str) -> None:
            try:
                barrier.wait()
                for i in range(per_thread):
                    manager.append_memory(SCOPE_GLOBAL, f"{tag}{i:03d}", allow_shared=True)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_append, args=(managers[0], "A")),
            threading.Thread(target=_append, args=(managers[1], "B")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = managers[0].load_memory(SCOPE_GLOBAL)
        missing = [
            f"{t}{i:03d}" for t in "AB" for i in range(per_thread) if f"{t}{i:03d}" not in final
        ]
        assert not errors, f"并发追加异常：{errors}"
        assert not missing, f"多管理器并发追加丢更新，缺失 {len(missing)} 条：{missing[:10]}"


class TestNoteRecalledManyCrossInstanceAtomicity:
    """🟡 跨实例并发召回写回不得丢更新——SELECT 必须在写事务内。

    修复前 SELECT 在事务外：两实例同读 recall_count=0，各自 +1 覆盖写 →
    终值 1（少记一次召回）；修复后 BEGIN IMMEDIATE 串行化读-改-写。
    """

    def test_two_instances_concurrent_recall_keeps_both_increments(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        import threading

        from bandi.memory.long_term import LongTermMemory, MemoryItem

        db = tmp_path / "lt.db"
        first = LongTermMemory(db)
        second = LongTermMemory(db)
        first.store(MemoryItem(item_id="m1", content="并发召回目标"))

        barrier = threading.Barrier(2)
        real_parse = LongTermMemory._parse_metadata

        def _synced_parse(raw: object) -> dict:
            # 破坏者视角：把「读到旧值」与「写回」之间的窗口撑开——修复前两线程
            # 同读 0 后各自覆盖写；修复后后到者在 BEGIN IMMEDIATE 排队，迟一步读到新值。
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                pass
            return real_parse(raw)

        monkeypatch.setattr(LongTermMemory, "_parse_metadata", staticmethod(_synced_parse))
        errors: list[Exception] = []

        def _recall(memory: LongTermMemory) -> None:
            try:
                memory.note_recalled_many(["m1"])
            except Exception as exc:  # noqa: BLE001 - 线程内异常带回主线程断言
                errors.append(exc)

        threads = [threading.Thread(target=_recall, args=(m,)) for m in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        try:
            assert not errors, f"并发召回抛错：{errors}"
            item = first.get("m1")
            assert item is not None
            assert item.metadata["recall_count"] == 2, (
                f"跨实例并发各 +1 必须终值 2（修复前丢更新为 1）：{item.metadata}"
            )
        finally:
            first.close()
            second.close()


# ==================== c1_memory_search_scope ====================
# 功能：契约——记忆检索的会话维度必须真正生效（并发工程复核 🟡-8）
#
# `tools/memory_tools.py` 调 `search_fts_ranked(query, limit)` **不传维度**
# 且 `MemorySearchTool` 未声明 `needs_context` → executor 不注入 extra →
# 检索只能命中全局层：既功能失效（看不到本员工/本项目长期记忆）
# 又是隔离漏口（能读到任意 agent 写进全局层的行）。
# 本契约钉：维度从会话元数据 → 工具上下文 → 检索调用 全链透传；
# 缺维度时归一为 None（不退化成 `IS NULL` 命中全局层）。


class _RecordingLongTerm:
    """记录检索入参的长期记忆替身。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def search_fts_ranked(
        self,
        query: str,
        limit: int = 5,
        project_id: str | None = None,
        employee_id: str | None = None,
    ) -> list[Any]:
        self.calls.append(
            {"query": query, "limit": limit, "project_id": project_id, "employee_id": employee_id}
        )
        return []


class _CtxProbeTool(BaseTool):
    """捕获 executor 注入的工具上下文（验证装配链）。"""

    name = "ctx_probe"
    description = "capture tool context"
    needs_context = True
    is_read_only = True
    is_concurrency_safe = True
    dedup_scope = "none"
    parameters: dict = {"type": "object", "additionalProperties": False, "properties": {}}

    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def execute(self, params: dict, context: Any = None) -> str:
        self.contexts.append(context)
        return "ok"


def r4xc1_ctx(extra: dict[str, Any] | None) -> ToolPermissionContext:
    return ToolPermissionContext(extra=extra)


class TestSearchPassesDimensions:
    """维度必须一路传到存储层。"""

    @pytest.mark.asyncio
    async def test_search_forwards_normalizes_and_defaults_dimensions(self) -> None:
        """核心：工具上下文带维度 → 检索调用收到同维度；无上下文 → 维度为 None；
        空串/空白维度必须归一为 None（否则退化成 `IS NULL` 命中全局层）；
        装配前提：不声明 needs_context 则 executor 不注入 extra（缺陷根因）。"""
        assert MemorySearchTool.needs_context is True

        long_term = _RecordingLongTerm()
        tool = MemorySearchTool(long_term=long_term)

        await tool.execute(
            {"query": "部署流程"},
            r4xc1_ctx({"project_id": "proj-a", "employee_id": "emp-1"}),
        )

        assert long_term.calls, "未发起检索"
        call = long_term.calls[0]
        assert call["project_id"] == "proj-a", f"project 维度未透传：{call}"
        assert call["employee_id"] == "emp-1", f"employee 维度未透传：{call}"

        long_term2 = _RecordingLongTerm()
        tool2 = MemorySearchTool(long_term=long_term2)

        await tool2.execute({"query": "x"}, None)

        call2 = long_term2.calls[0]
        assert call2["project_id"] is None and call2["employee_id"] is None

        long_term3 = _RecordingLongTerm()
        tool3 = MemorySearchTool(long_term=long_term3)

        await tool3.execute({"query": "x"}, r4xc1_ctx({"project_id": "", "employee_id": "   "}))

        call3 = long_term3.calls[0]
        assert call3["project_id"] is None, f"空串未归一：{call3}"
        assert call3["employee_id"] is None, f"空白未归一：{call3}"


class TestExecutorInjectsDimensions:
    """装配链：会话元数据 → 工具上下文 extra。"""

    @pytest.mark.asyncio
    async def test_executor_injects_or_omits_dimensions_by_meta(self) -> None:
        """**端到端装配**：executor 必须把会话维度注入 needs_context 工具的 extra；
        边界：会话元数据缺维度 → 注入空串（工具侧归一为 None），不得报错。"""
        probe = _CtxProbeTool()
        registry = ToolRegistry()
        registry.register_instance(probe)
        executor = ToolExecutor(registry=registry, max_concurrency=8)
        executor.bind_session_meta(
            {"session_id": "s1", "project_id": "proj-a", "employee_id": "emp-1"}
        )

        await executor.execute([ToolCall(id="c1", name="ctx_probe", arguments={})])

        assert probe.contexts, "工具未被执行"
        extra = probe.contexts[0].extra or {}
        assert extra.get("project_id") == "proj-a", f"project 维度未注入：{extra}"
        assert extra.get("employee_id") == "emp-1", f"employee 维度未注入：{extra}"

        probe2 = _CtxProbeTool()
        registry2 = ToolRegistry()
        registry2.register_instance(probe2)
        executor2 = ToolExecutor(registry=registry2, max_concurrency=8)
        executor2.bind_session_meta({"session_id": "s1"})

        await executor2.execute([ToolCall(id="c1", name="ctx_probe", arguments={})])

        extra2 = probe2.contexts[0].extra or {}
        assert not extra2.get("project_id"), f"缺维度时不应有值：{extra2}"
        assert not extra2.get("employee_id")


# ==================== c2_agent_identity ====================
# 功能：契约——Agent 实例标识进上下文（并发工程 批次3 / ）
#
# `agent/loop.py:481` 读 `ctx.meta["agent_id"]` 写入结构化日志上下文，但**全仓无
# 任何写入点**（工厂只写 `agent_role`）→ 多 Agent 并发时日志的 agent 维度恒为
# 空串，**无法按 Agent 实例归因**（只能追到角色）。本契约钉死：
# ① `agent_id` 必须非空；② 同一角色的**不同会话实例必须可区分**（并发归因的前提）；
# ③ 无会话时回落角色名（降级但不空）。


def r5xc2_ctx(**kwargs: object):
    cfg = BandiConfig()
    return build_agent_context(user_input="x", bandi_config=cfg, **kwargs)


def test_agent_id_populated_with_role_and_never_empty() -> None:
    """默认路径必须写入非空 agent_id（此前恒为空串）；agent_id 需保留角色信息，
    便于人工阅读日志；**破坏者视角**：角色为空串时 agent_id 仍不得为空。"""
    ctx = r5xc2_ctx()
    assert ctx.meta.get("agent_id"), "agent_id 未写入（日志 agent 维度会恒空）"

    ctx2 = r5xc2_ctx(session_id="sess-12345678", agent_role="reviewer")
    assert "reviewer" in str(ctx2.meta.get("agent_id"))

    ctx3 = r5xc2_ctx(agent_role="")
    assert ctx3.meta.get("agent_id"), "空角色下 agent_id 仍为空串"


def test_agent_id_distinguishes_sessions_and_falls_back_without_session() -> None:
    """**并发归因前提**：同一角色的两个会话实例必须有不同 agent_id；
    无会话时回落角色名——降级但不为空。"""
    a = r5xc2_ctx(session_id="sess-aaaaaaaa", agent_role="engineer")
    b = r5xc2_ctx(session_id="sess-bbbbbbbb", agent_role="engineer")

    id_a, id_b = a.meta.get("agent_id"), b.meta.get("agent_id")
    assert id_a and id_b
    assert id_a != id_b, f"同角色不同会话的 agent_id 相同（{id_a!r}）——并发无法归因"

    ctx = r5xc2_ctx(agent_role="worker")
    assert ctx.meta.get("agent_id") == "worker"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_child_identity_composes_falls_back_distinguishes_and_avoids_dangling_at() -> None:
    """子身份由父 id + 会话尾缀构成，父链原样回传；无父时回落 `subagent@尾缀`（降级不空）；
    同一父下并发子会话必须产出不同子身份（并发归因前提）；
    空会话（session_id=""）不得产出悬空 `@`。"""
    from bandi.agent.subagent import build_child_agent_identity

    child, parent = build_child_agent_identity("eng@12345678", "subagent-42abcdef12")
    assert child == "eng@12345678::sub@abcdef12"
    assert parent == "eng@12345678"

    child_no_parent, parent_empty = build_child_agent_identity("", "subagent-42abcdef12")
    assert child_no_parent and child_no_parent.startswith("subagent@"), (
        f"子身份为空：{child_no_parent!r}"
    )
    assert parent_empty == "", "无父时不得伪造父链"

    a, _ = build_child_agent_identity("", "subagent-aaaaaaaaaaaa")
    b, _ = build_child_agent_identity("", "subagent-bbbbbbbbbbbb")
    assert a != b, f"并发子身份相同：{a!r}"

    child_empty, parent_none = build_child_agent_identity("", "")
    assert child_empty == "subagent", f"空会话子身份不得带悬空 @：{child_empty!r}"
    assert parent_none == "", "无父时不得伪造父链"

    child2, _ = build_child_agent_identity("eng@12345678", "")
    assert child2 == "eng@12345678::sub", f"有父空会话不得带悬空 @：{child2!r}"


def test_run_subagent_writes_identity_meta_and_executor_child_agent_id(
    monkeypatch: object,
) -> None:
    """接线（DoD-12）：真实 `_run_subagent`（AgentLoop 打桩）→ 子 ctx.meta 写身份与父链；
    生产链：子代理为自身工具面构造的 executor 必须持有子代理自己的 agent_id
    （孙代理的父链=子代理，而非 "default"）。"""
    import asyncio

    import bandi.agent._assembly as assembly_mod
    import bandi.agent.checkpoint as ckpt_mod
    import bandi.agent.loop as loop_mod
    from bandi.agent.subagent import (
        SubagentConfig,
        SubagentManager,
        SubagentSession,
    )
    from bandi.llm.mock import MockProvider
    from bandi.tools.registry import ToolRegistry

    captured: dict[str, str] = {}

    class _NoStore:
        def close(self) -> None: ...
        def delete_by_session(self, session_id: str) -> None: ...

    class _StubLoop:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def run(self, ctx: object, **kwargs: object) -> object:
            from bandi.agent.base import TerminalReason

            meta = getattr(ctx, "meta", {})
            captured["agent_id"] = str(meta.get("agent_id") or "")
            captured["parent_agent_id"] = str(meta.get("parent_agent_id") or "")
            executor = getattr(ctx, "tool_executor", None)
            captured["executor_agent_id"] = str(getattr(executor, "agent_id", "") or "")
            ctx.final_message = "done"  # type: ignore[attr-defined]
            return TerminalReason.COMPLETED

    def _no_idem(store: Any) -> Any:
        return store

    monkeypatch.setattr(ckpt_mod, "CheckpointStore", lambda *a, **k: _NoStore())  # type: ignore[attr-defined]
    monkeypatch.setattr(loop_mod, "AgentLoop", _StubLoop)  # type: ignore[attr-defined]
    monkeypatch.setattr(assembly_mod, "ensure_subagent_idempotency_store", _no_idem)  # type: ignore[attr-defined]

    manager = SubagentManager(provider=MockProvider(), parent_agent_id="eng@12345678")
    session = SubagentSession(session_id="subagent-42abcdef12")
    asyncio.run(
        manager._run_subagent(
            "t",
            SubagentConfig(max_turns=1, timeout_sec=5),
            session,
            tool_registry=ToolRegistry(),
        )
    )

    assert captured["agent_id"] == "eng@12345678::sub@abcdef12"
    assert captured["parent_agent_id"] == "eng@12345678"
    assert captured.get("executor_agent_id") == "eng@12345678::sub@abcdef12", (
        f"子代理 executor 未持真实子身份：{captured.get('executor_agent_id')!r}"
    )


def test_subagent_tool_passes_parent_identity(monkeypatch: object) -> None:
    """接线：SubAgentTool 从工具上下文 extra 取父 agent_id 并下传 manager。"""
    import asyncio

    import bandi.agent.subagent_tool as st_mod
    from bandi.agent.subagent import SubagentResult
    from bandi.tools.base import ToolPermissionContext

    captured: dict[str, Any] = {}

    class _CaptureManager:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def spawn_many(self, *args: Any, **kwargs: Any) -> list[SubagentResult]:
            return [SubagentResult(success=True, summary="s", message="m", total_tokens=1)]

        def cancel_all(self) -> None: ...

        def close(self) -> None: ...

    monkeypatch.setattr(st_mod, "SubagentManager", _CaptureManager)  # type: ignore[attr-defined]
    tool = st_mod.SubAgentTool(provider=None, tool_registry=None)
    context = ToolPermissionContext(role="engineer", extra={"agent_id": "eng@12345678"})
    asyncio.run(tool.execute({"tasks": ["t"]}, context))

    assert captured.get("parent_agent_id") == "eng@12345678", (
        f"父身份未下传：parent_agent_id={captured.get('parent_agent_id')!r}"
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_factory_executor_carries_real_agent_id() -> None:
    """生产链（真实 factory 装配）：executor 的 agent_id 必须是 ctx.meta 的真实实例 id。

    红点：factory 构造 ToolExecutor 时真实 id 尚未生成（晚于构造），executor 恒为
    "default" → 子代理工具的父链（context.extra["agent_id"]）在生产恒为 "default"。
    """
    ctx = r5xc2_ctx(session_id="sess-12345678", agent_role="engineer")
    real_id = str(ctx.meta.get("agent_id") or "")
    assert real_id and real_id != "default"

    executor = ctx.tool_executor
    assert getattr(executor, "agent_id", "") == real_id, (
        f"executor.agent_id={getattr(executor, 'agent_id', '')!r} != meta={real_id!r}"
    )


# ---------------------------------------------------------------------------
# 续：子代理 hooks 链（per-child runner）
# ---------------------------------------------------------------------------


def _run_child_with_probe(
    monkeypatch: object,
    runner: object,
    captured: dict[str, object],
    manager_cls: object = None,
) -> None:
    """功能：走真实 `_run_subagent`，让子执行器执行探针工具并回填 captured。

    manager_cls 提供时，仅在 hooks 执行阶段替换 `SubagentManager`（外层 manager
    先用真实类构造，避免替换后无法进入 `_run_subagent`）。
    """
    import asyncio

    import bandi.agent._assembly as assembly_mod
    import bandi.agent.checkpoint as ckpt_mod
    import bandi.agent.loop as loop_mod
    import bandi.agent.subagent as subagent_mod
    from bandi.agent.subagent import (
        SubagentConfig,
        SubagentManager,
        SubagentSession,
    )
    from bandi.core.message import ToolCall
    from bandi.llm.mock import MockProvider
    from bandi.tools.base import BaseTool, ToolCategory
    from bandi.tools.registry import ToolRegistry

    class _Probe(BaseTool):
        name = "probe"
        description = "capture executor extra probe"
        category = ToolCategory.UTIL
        is_read_only = True

        async def execute(self, params: dict, context: object = None):  # type: ignore[override]
            extra = getattr(context, "extra", None) or {}
            captured["hook_runner"] = extra.get("hook_runner")
            return "ok"

    class _NoStore:
        def close(self) -> None: ...
        def delete_by_session(self, session_id: str) -> None: ...

    class _StubLoop:
        def __init__(self, *args: object, **kwargs: object) -> None: ...

        async def run(self, ctx: object, **kwargs: object) -> object:
            from bandi.agent.base import TerminalReason

            await ctx.tool_executor.execute(  # type: ignore[attr-defined]
                [ToolCall(id="t1", name="probe", arguments={})]
            )
            ctx.final_message = "done"  # type: ignore[attr-defined]
            return TerminalReason.COMPLETED

    def _no_idem(store: object) -> object:
        return store

    monkeypatch.setattr(ckpt_mod, "CheckpointStore", lambda *a, **k: _NoStore())  # type: ignore[attr-defined]
    monkeypatch.setattr(loop_mod, "AgentLoop", _StubLoop)  # type: ignore[attr-defined]
    monkeypatch.setattr(assembly_mod, "ensure_subagent_idempotency_store", _no_idem)  # type: ignore[attr-defined]

    registry = ToolRegistry()
    registry.register_instance(_Probe())
    manager = SubagentManager(
        provider=MockProvider(),
        hook_runner=runner,
        parent_agent_id="eng@12345678",
    )
    if manager_cls is not None:
        monkeypatch.setattr(subagent_mod, "SubagentManager", manager_cls)  # type: ignore[attr-defined]
    session = SubagentSession(session_id="subagent-42abcdef12")
    asyncio.run(
        manager._run_subagent(
            "t", SubagentConfig(max_turns=1, timeout_sec=5), session, tool_registry=registry
        )
    )


def test_subagent_executor_carries_hook_runner(monkeypatch: object) -> None:
    """F3a 契约：子代理为自身工具面构造的 executor 必须透传 hook_runner——
    否则子代理内派生的孙代理工具面绕过用户 PreToolUse deny（安全链二代缺口）。"""
    from bandi.tools.hooks import HookRunner

    captured: dict[str, object] = {}
    parent_runner = HookRunner([])
    _run_child_with_probe(monkeypatch, parent_runner, captured)

    child_runner = captured.get("hook_runner")
    assert child_runner is not None, "子代理 executor 未透传 hook_runner（孙代理安全链断链）"
    assert child_runner is not parent_runner, "子代理必须持有 per-child runner（不得复用父实例）"


def test_subagent_hook_derived_grandchild_uses_child_parent_id(monkeypatch: object) -> None:
    """F3b 契约：子代理内 hooks 派生的孙代理 parent_agent_id == 子代理 id
    （共享父 runner 实例会把血缘记成主链 id）。"""
    from bandi.agent.subagent import SubagentResult
    from bandi.llm.mock import MockProvider
    from bandi.tools.hooks import HOOK_PRE_TOOL_USE, HookRule, HookRunner

    captured: dict[str, object] = {}

    class _CaptureManager:
        def __init__(self, **kwargs: object) -> None:
            captured["derived_parent_agent_id"] = str(kwargs.get("parent_agent_id") or "")

        async def spawn(self, *args: object, **kwargs: object) -> SubagentResult:
            return SubagentResult(success=True, message='{"permissionDecision": "allow"}')

        def close(self) -> None: ...

    parent_runner = HookRunner(
        [HookRule(event=HOOK_PRE_TOOL_USE, action_type="agent", prompt="review")],
        provider=MockProvider(),
        agent_id="eng@12345678",
    )
    _run_child_with_probe(monkeypatch, parent_runner, captured, manager_cls=_CaptureManager)

    assert captured.get("derived_parent_agent_id") == "eng@12345678::sub@abcdef12", (
        f"孙代理父链错位：{captured.get('derived_parent_agent_id')!r}"
    )


def test_jsonl_entry_carries_parent_agent_id(tmp_path: object) -> None:
    """JSONL 落地：桥转发的日志条目必须携带 parent_agent_id（顶层字段）。"""
    import json
    import logging
    from pathlib import Path

    from bandi.observability.context import bind_log_context
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge

    log_dir = Path(str(tmp_path))
    logger = Logger(log_dir, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.parent_chain")
    try:
        with bind_log_context(session_id="s-1", agent_id="child@x", parent_agent_id="eng@y"):
            logging.getLogger("bandi.parent_chain").info("with parent")
    finally:
        handler.close()
        logger.close()

    lines = [
        line
        for line in (log_dir / "bandi.log").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert lines, "日志未落盘"
    entry = json.loads(lines[0])
    assert entry.get("parent_agent_id") == "eng@y", f"parent_agent_id 未落顶层：{entry}"


# ==================== c2_checkpoint_atomicity ====================


def test_checkpoint_store_upserts_wal_and_migrates_legacy(tmp_path: Path) -> None:
    store = CheckpointStore(str(tmp_path / "ckpt.db"))
    store.save(Checkpoint(session_id="s1", turn_index=3, state_json='{"a":1}'))
    store.save(Checkpoint(session_id="s1", turn_index=3, state_json='{"a":2}'))
    rows = store.list_by_session("s1")
    assert len(rows) == 1, f"(session,turn) 唯一约束必须生效（实际 {len(rows)}）"
    cp = store.load(rows[0]["id"])
    assert cp is not None and json.loads(cp.state_json)["a"] == 2, "同键必须最新覆盖"

    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", f"checkpoint 库必须 WAL（实际 {mode}）"

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE checkpoints (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            messages_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO checkpoints VALUES ('old-1','s1',5,'{}','', '2026-08-30T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    legacy_store = CheckpointStore(str(db))
    legacy_store.save(Checkpoint(session_id="s1", turn_index=5, state_json='{"new":1}'))
    rows2 = legacy_store.list_by_session("s1")
    assert len(rows2) == 1, "旧库迁移后同键仅保留一行"


def test_command_id_roundtrip_flows_into_events_and_restores(tmp_path: Path) -> None:
    """command_id 必须持久化往返；checkpoint 写入后事件自动携带同一 command_id；
    恢复 checkpoint 后新 emit 事件续用同一 command_id（崩溃重放幂等对齐）。"""
    import asyncio

    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType
    from bandi.memory.session.store import SessionStore

    store = CheckpointStore(str(tmp_path / "ckpt_cmdid.db"))
    store.save(
        Checkpoint(
            session_id="s2",
            turn_index=1,
            state_json="{}",
            command_id="cmd-abc-123",
        )
    )
    rows = store.list_by_session("s2")
    cp = store.load(rows[0]["id"])
    assert cp is not None and cp.command_id == "cmd-abc-123", "command_id 必须持久化往返"

    bus = EventBus()
    loop = AgentLoop(checkpoint_store=store)
    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=bus,
    )
    ctx.meta["session_id"] = "s-cmd"
    ctx.turn_index = 1
    captured: list[Any] = []

    async def _on(ev: Event) -> None:
        captured.append(ev)

    async def run() -> None:
        await bus.subscribe(EventType.STATE_CHANGED, _on)
        loop._write_checkpoint(ctx)
        await bus.emit(Event(type=EventType.STATE_CHANGED, data={}))

    asyncio.run(run())
    rows_cmd = store.list_by_session("s-cmd")
    cp_cmd = store.load(rows_cmd[0]["id"])
    assert cp_cmd is not None and cp_cmd.command_id, "checkpoint 必须携带 command_id"
    assert len(captured) == 1 and captured[0].command_id == cp_cmd.command_id, (
        "checkpoint 后的事件必须携带同一 command_id"
    )

    restore_store = CheckpointStore(str(tmp_path / "ckpt_restore.db"))
    restore_store.save(
        Checkpoint(
            session_id="s-restore",
            turn_index=2,
            state_json='{"current_state": "thinking"}',
            command_id="cmd-chain-9",
        )
    )
    sess = SessionStore(str(tmp_path / "sess_restore.db"))
    sess.create_session(mode="execute", session_id="s-restore")
    ctx2 = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx2.meta["session_id"] = "s-restore"
    captured2: list[Any] = []

    async def _on2(ev: Event) -> None:
        captured2.append(ev)

    async def run2() -> None:
        from bandi.core.agent_runner import _session_restore

        _session_restore("s-restore", sess, ctx2, checkpoint_store=restore_store)
        await ctx2.bus.subscribe(EventType.STATE_CHANGED, _on2)
        await ctx2.bus.emit(Event(type=EventType.STATE_CHANGED, data={}))

    asyncio.run(run2())
    assert len(captured2) == 1 and captured2[0].command_id == "cmd-chain-9", (
        f"恢复后事件必须续用 checkpoint command_id（实际 {captured2[0].command_id if captured2 else None}）"
    )
    store.close()
    restore_store.close()


def test_timeout_branch_persists_checkpoint(tmp_path: Path) -> None:
    from bandi.agent.base import ExecutionContext, TerminalReason
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus

    ckpt_store = CheckpointStore(str(tmp_path / "ckpt_timeout.db"))
    loop = AgentLoop(checkpoint_store=ckpt_store)
    ctx = ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )
    ctx.meta["session_id"] = "s-timeout"
    ctx.turn_index = 2
    loop._session_timeout_sec = 30

    loop._on_timeout(ctx, turns_taken=2)

    rows = ckpt_store.list_by_session("s-timeout")
    assert len(rows) == 1, "timeout 分支必须落盘 checkpoint"
    assert ctx.terminal_reason == TerminalReason.TIMEOUT
    assert (
        ctx.final_message is not None and "30" in ctx.final_message and "超时" in ctx.final_message
    )
    ckpt_store.close()


def test_subagent_loop_has_checkpoint_store() -> None:
    from bandi.agent.subagent import SubagentManager

    agent = SubagentManager()
    assert agent._loop_checkpoint_store is not None, (
        "子 Agent 必须持有 checkpoint store（handoff 边界）"
    )


# ==================== c3_binding_chain ====================


class _FakeAccountStore:
    """最小账户库替身：只需提供 list_user_roles。"""

    def __init__(self, bindings: dict[str, list[str]] | None = None) -> None:
        self._bindings = bindings or {}
        self.queries: list[str] = []

    def list_user_roles(self, user_id: str) -> list[str]:
        self.queries.append(user_id)
        return list(self._bindings.get(user_id, []))


def _capture_registered_tier(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 register_builtin_role 换成捕获器——观测「最终注册了哪个权限档」。"""
    captured: dict[str, Any] = {}

    def _fake_register(engine: Any, tier: str) -> None:
        captured["tier"] = tier

    import bandi.core.permission.roles as roles_module

    monkeypatch.setattr(roles_module, "register_builtin_role", _fake_register)
    return captured


# ---------------------------------------------------------------------------
# 1) _resolve_account_store 语义
# ---------------------------------------------------------------------------


def test_resolve_account_store_empty_explicit_fallback_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """契约 2：user_id 为空 → 不解析、不触碰默认库（零副作用保证）；
    契约 3：显式传入的 store 优先，不得被默认库顶替；
    契约 1：只给 user_id、不给 store → 必须自解析默认库（原缺陷在此）；
    契约 4：默认库构建失败 → fail-open（返回 None，不崩）。"""
    called = {"n": 0}

    def _boom() -> Any:
        called["n"] += 1
        raise AssertionError("user_id 为空时不得触碰默认账户库")

    monkeypatch.setattr(factory_module, "_default_account_store", _boom)

    assert _resolve_account_store("", None) is None
    assert called["n"] == 0, "空 user_id 不得触发默认库构建"

    sentinel = _FakeAccountStore()

    def _boom_explicit() -> Any:
        raise AssertionError("已显式传 store 时不得再构建默认库")

    monkeypatch.setattr(factory_module, "_default_account_store", _boom_explicit)

    assert _resolve_account_store("u1", sentinel) is sentinel

    fallback = _FakeAccountStore()
    monkeypatch.setattr(factory_module, "_default_account_store", lambda: fallback)

    assert _resolve_account_store("u1", None) is fallback

    def _raise_oserror() -> Any:
        raise OSError("account.db unavailable")

    monkeypatch.setattr(factory_module, "_default_account_store", _raise_oserror)

    assert _resolve_account_store("u1", None) is None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_policy_engine_binding_paths_with_and_without_explicit_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**核心契约**：只传 user_id，岗位绑定必须真正改变注册的权限档；
    回归：显式传 store 的老路径不得被改动破坏；
    回归：无 user_id → 回落 agent_role 经 role_map（与改造前一致）。

    原缺陷：`seat_executor` 只传 user_id 不传 store → `user_id and account_store
    is not None` 恒假 → 整段跳过 → 注册的是回落档而非绑定档。
    """
    monkeypatch.setattr(
        factory_module,
        "_default_account_store",
        lambda: _FakeAccountStore({"u1": ["ops_lead"]}),
    )
    captured = _capture_registered_tier(monkeypatch)

    build_policy_engine(object(), agent_role="user", role_map={"ops_lead": "admin"}, user_id="u1")

    assert captured["tier"] == "admin", (
        f"绑定档必须生效（期望 admin，实得 {captured.get('tier')!r}）"
    )

    captured2 = _capture_registered_tier(monkeypatch)

    build_policy_engine(
        object(),
        agent_role="user",
        role_map={"ops_lead": "power_user"},
        user_id="u1",
        account_store=_FakeAccountStore({"u1": ["ops_lead"]}),
    )

    assert captured2["tier"] == "power_user"

    captured3 = _capture_registered_tier(monkeypatch)

    build_policy_engine(object(), agent_role="member", role_map={}, user_id="")

    assert captured3["tier"] == "member"


def test_policy_engine_unmapped_and_multiple_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约 5（破坏者视角）：绑定存在但 role_map 无映射 → 回落 agent_role，不得越权；
    多岗位绑定取最高档（既有语义不得被兜底改动破坏）。"""
    monkeypatch.setattr(
        factory_module,
        "_default_account_store",
        lambda: _FakeAccountStore({"u1": ["ghost_role"]}),
    )
    captured = _capture_registered_tier(monkeypatch)

    build_policy_engine(object(), agent_role="user", role_map={"ops_lead": "admin"}, user_id="u1")

    assert captured["tier"] == "user", f"未映射绑定不得提升权限：{captured.get('tier')!r}"

    monkeypatch.setattr(
        factory_module,
        "_default_account_store",
        lambda: _FakeAccountStore({"u1": ["viewer", "ops_lead"]}),
    )
    captured2 = _capture_registered_tier(monkeypatch)

    build_policy_engine(
        object(),
        agent_role="user",
        role_map={"viewer": "readonly", "ops_lead": "admin"},
        user_id="u1",
    )

    assert captured2["tier"] == "admin"


# ==================== c3_company_identity ====================
# 功能：契约——公司链路发起用户标识解析（并发工程 批次3 / ）
#
# `company/seat_executor.py:284` 调 `run_agent(...)` 时**不透传 `user_id`**
# （该文件全文零 user_id），而 `factory.run_agent:1002` / `agent_runner.run_agent_turn:819`
# 均有该形参 → 公司多 Agent 链路的执行面身份回落 `_agent_id`
# 本契约钉死解析行为：优先取卡上声明的发起人，缺省回落本地默认用户（本地单机形态）。


def test_resolve_requester_user_id_declared_fallback_and_guards() -> None:
    """卡上声明了发起人时以声明为准；未声明时回落本地默认用户（本地单机形态）；
    卡对象没有 metadata 属性时也不得抛错（防御）；
    **破坏者视角**：声明为空串/纯空白时仍必须回落——返回空串等于没透传（原缺陷复现）。"""
    item = SimpleNamespace(metadata={"requester_user_id": "u-42"})
    assert resolve_requester_user_id(item) == "u-42"

    item_fallback = SimpleNamespace(metadata={})
    assert resolve_requester_user_id(item_fallback) == DEFAULT_LOCAL_USER_ID

    assert resolve_requester_user_id(SimpleNamespace()) == DEFAULT_LOCAL_USER_ID

    item_empty = SimpleNamespace(metadata={"requester_user_id": ""})
    assert resolve_requester_user_id(item_empty), "返回空串 → 执行面身份仍会回落 _agent_id"

    item_blank = SimpleNamespace(metadata={"requester_user_id": "   "})
    assert resolve_requester_user_id(item_blank) == DEFAULT_LOCAL_USER_ID


# ==================== c3_tools ====================
# 契约原则：失败命令必须显式失败（非零退出码不得当成功返回）；
# 熔断半开期一次失败必须立即回 OPEN（半开期无保护直通禁止）。


class _FakeStream:
    # 功能：模拟子进程输出流（read 分块返回，EOF 返回空）
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        if not self._data:
            return b""
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


def _mock_proc(returncode: int, out: bytes, err: bytes) -> MagicMock:
    # 功能：构造读循环形态的子进程 mock（stdout/stderr 流 + returncode + pid）
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _FakeStream(out)
    proc.stderr = _FakeStream(err)
    proc.pid = 4321
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def test_contract_bash_exit_code_paths() -> None:
    """非零退出码不得被 stderr/stdout 内容掩盖（失败命令须显式失败）；0 退出返回输出。"""
    from bandi.tools.bash import BashTool

    tool = BashTool()
    proc = _mock_proc(3, b"", b"boom")
    with patch("asyncio.create_subprocess_exec", return_value=proc):
        with pytest.raises(RuntimeError, match="exit 3"):
            asyncio.run(tool.execute({"command": "ls /nonexistent"}))

    proc2 = _mock_proc(2, b"partial output", b"")
    with patch("asyncio.create_subprocess_exec", return_value=proc2):
        with pytest.raises(RuntimeError, match="exit 2"):
            asyncio.run(tool.execute({"command": "echo hi"}))

    proc3 = _mock_proc(0, b"file1\nfile2\n", b"")
    with patch("asyncio.create_subprocess_exec", return_value=proc3):
        result = asyncio.run(tool.execute({"command": "ls"}))
    assert "file1" in result and "file2" in result


def test_contract_circuit_half_open_failure_reopens() -> None:
    mw = CircuitBreakerMiddleware(threshold=2, recovery_timeout=60.0)
    call = ToolCall(id="t1", name="web_fetch", arguments={"url": "https://x"})
    ctx = type("Ctx", (), {"_retrying": False})()  # type: ignore[arg-type]
    fail_result = ToolResult(tool_call_id="t1", tool_name="web_fetch", success=False, output="fail")

    # 2 次失败 → OPEN
    mw._failure_counts["web_fetch"] = 1
    mw._last_failure["web_fetch"] = 0.0
    r1 = asyncio.run(mw.after_execute(ctx, call, fail_result))
    assert r1.success is False
    assert mw._get_state("web_fetch") == CircuitState.OPEN, "2 次失败必须 OPEN"

    # 过 recovery_timeout → HALF_OPEN
    import time

    mw._last_failure["web_fetch"] = time.monotonic() - 120.0
    allowed = asyncio.run(mw.before_execute(ctx, call))
    assert allowed.allow is True, "超时后应放行一次探测（HALF_OPEN）"
    assert mw._get_state("web_fetch") == CircuitState.HALF_OPEN

    # HALF_OPEN 探测失败 → 必须立即回 OPEN（重新计时）
    r2 = asyncio.run(mw.after_execute(ctx, call, fail_result))
    assert r2.success is False
    assert mw._get_state("web_fetch") == CircuitState.OPEN, (
        "HALF_OPEN 探测失败必须立即回 OPEN（半开期无保护直通禁止）"
    )
    # 回 OPEN 后立即拦截
    blocked = asyncio.run(mw.before_execute(ctx, call))
    assert blocked.allow is False, "回 OPEN 后必须拦截"


def test_contract_read_grep_and_doc_tools_reject_oversized_file(tmp_path) -> None:
    """超大文件必须被 read/grep/doc 工具族拒绝——拒绝理由含大小语义（不得全量读取/OOM）。"""
    import os

    from bandi.tools.doc.csv_processor import CSVProcessorTool
    from bandi.tools.doc.text_processor import TextFileProcessorTool
    from bandi.tools.file_edit import FileEditTool
    from bandi.tools.file_ops import FileReadTool
    from bandi.tools.fs_tool import _MAX_FILE_SIZE
    from bandi.tools.grep_tool import GrepTool

    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (_MAX_FILE_SIZE + 1024))
    big_log = tmp_path / "big.log"
    big_log.write_bytes(b"x" * (_MAX_FILE_SIZE + 1024))

    read_tool = FileReadTool()
    read_rejected = False
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        try:
            asyncio.run(read_tool.execute({"path": str(big)}))
        except (ValueError, RuntimeError) as e:
            read_rejected = True
            assert "size" in str(e).lower() or "max" in str(e).lower(), (
                f"拒绝理由应含大小语义，实际: {e}"
            )
    assert read_rejected, "超大文件必须被拒绝"

    grep_tool = GrepTool()
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        result = asyncio.run(grep_tool.execute({"pattern": "needle", "path": str(tmp_path)}))
    assert "big.log" not in result, "超大文件不得被 grep 全量读取"

    for cls in (TextFileProcessorTool, CSVProcessorTool):
        doc_tool = cls()
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
        ):
            doc_result = asyncio.run(doc_tool.execute({"operation": "read", "file_path": str(big)}))
        assert doc_result.get("success") is False, (
            f"{cls.__name__} 超大文件必须拒绝（success=False），实际: {doc_result}"
        )
        error = str(doc_result.get("error", ""))
        assert "size" in error.lower() or "max" in error.lower(), (
            f"{cls.__name__} 拒绝理由应含大小语义，实际: {error}"
        )

    ws = tmp_path / "ws"
    ws.mkdir()
    big_ws = ws / "big.txt"
    big_ws.write_bytes(b"x" * (_MAX_FILE_SIZE + 1024))
    edit_tool = FileEditTool()
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", ws),
        patch.dict(os.environ, {"BANDI_WORKSPACE_ROOT": str(ws)}),
    ):
        try:
            asyncio.run(
                edit_tool.execute({"file_path": str(big_ws), "old_string": "x", "new_string": "y"})
            )
        except ValueError as e:
            assert "size" in str(e).lower() or "max" in str(e).lower(), (
                f"FileEditTool 拒绝理由应含大小语义，实际: {e}"
            )
        else:
            raise AssertionError("FileEditTool 超大文件必须拒绝（抛 ValueError）")


def test_contract_grep_include_glob_escapes_meta(tmp_path) -> None:
    """
    而非「任意单字符」，`[ab].py` 中 `[` 开始字符类（语义错误）
    `?.txt` 开头 `?` 还会引发 re.error 整次 grep 崩溃。"""
    from bandi.tools.grep_tool import GrepTool

    (tmp_path / "file1.py").write_text("needle1\n", encoding="utf-8")
    (tmp_path / "file2.py").write_text("needle2\n", encoding="utf-8")
    (tmp_path / "file12.py").write_text("needle12\n", encoding="utf-8")
    (tmp_path / "ab.py").write_text("needleab\n", encoding="utf-8")

    tool = GrepTool()
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        result = asyncio.run(
            tool.execute({"pattern": "needle", "path": str(tmp_path), "include": "file?.py"})
        )
    # `file?.py` 应匹配 file1.py/file2.py（单字符），不匹配 file12.py（双字符）
    assert "file1.py" in result and "file2.py" in result, "file?.py 应匹配单字符文件名"
    assert "file12.py" not in result, "`?` 只匹配单字符，file12.py 不得匹配"
    assert "ab.py" not in result, "非 file? 前缀的文件不得匹配"

    # `[ab].py` 开头 `[` 未转义会触发 re.error（unterminated character set）——
    # 修复后 `[` 被转义为字面 `[`，仅匹配字面文件名 `[ab].py`
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        result2 = asyncio.run(
            tool.execute({"pattern": "needle", "path": str(tmp_path), "include": "[ab].py"})
        )
    assert "ab.py" not in result2, "`[ab].py` 的 `[` 应作字面字符，不得当字符类匹配 ab.py"


def test_contract_validate_params_respects_additional_properties() -> None:
    """
    （MCP 远端工具合法扩展参数被拒，功能断裂）。"""
    from bandi.tools.base import BaseTool, ToolCategory

    class _RemoteTool(BaseTool):
        name = "remote_op"
        description = "remote"
        category = ToolCategory.INTEGRATION
        parameters = {
            "type": "object",
            "additionalProperties": True,
            "properties": {"known": {"type": "string"}},
        }

        async def execute(self, params, context=None):  # type: ignore[override]
            return "ok"

    tool = _RemoteTool()
    errors = tool.validate_params({"known": "a", "extra_future_field": 42})
    assert errors == [], f"additionalProperties=true 时扩展参数不得报未知参数，实际: {errors}"

    class _StrictTool(BaseTool):
        name = "strict_op"
        description = "strict"
        category = ToolCategory.UTIL
        parameters = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"known": {"type": "string"}},
        }

        async def execute(self, params, context=None):  # type: ignore[override]
            return "ok"

    strict = _StrictTool()
    assert strict.validate_params({"unknown": 1}), "additionalProperties=false 时未知参数必须拒绝"


def test_contract_mcp_dynamic_tools_not_low_risk() -> None:
    """
    远端任意能力对 agent 无条件可用是权限绕过面；默认必须要求确认通道。"""
    from unittest.mock import AsyncMock, MagicMock

    from bandi.tools.mcp_client import MCPClient, register_mcp_tools
    from bandi.tools.registry import ToolRegistry

    transport = MagicMock()
    transport.available = True
    client = MCPClient("remote", transport)
    client._initialized = True
    client.list_tools = AsyncMock(
        return_value=[{"name": "exec_anything", "description": "run", "inputSchema": {}}]
    )

    registry = ToolRegistry()
    import asyncio as _asyncio

    _asyncio.run(register_mcp_tools(registry, client, prefix=None))

    tool = registry.get("remote_exec_anything")
    assert tool is not None, "MCP 工具必须注册"
    assert tool.risk_level != "low", (
        f"MCP 动态工具 risk_level 必须不是 low（免确认直通），实际: {tool.risk_level}"
    )


def test_contract_stdio_transport_drains_stderr() -> None:
    """
    stderr=PIPE 不消费时 MCP server 写满管道缓冲即阻塞挂起
    （子进程整体卡死，请求超时）。"""
    from bandi.tools.mcp_client import StdioTransport

    async def _run() -> None:
        transport = StdioTransport("echo")

        stderr_reader = asyncio.StreamReader()
        stderr_reader.feed_data(b"log line 1\nlog line 2\n")
        stderr_reader.feed_eof()

        stdout_reader = asyncio.StreamReader()
        stdout_reader.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{}}\n')
        stdout_reader.feed_eof()

        proc = AsyncMock()
        proc.stdout = stdout_reader
        proc.stderr = stderr_reader
        proc.stdin = None
        proc.returncode = None

        async def _set_returncode() -> None:
            proc.returncode = 0

        proc.wait.side_effect = _set_returncode

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await transport.connect()
            await asyncio.sleep(0.05)
            await transport.disconnect()

        assert stderr_reader.at_eof(), "stderr 必须被消费到 EOF（drain 任务读取）"

    asyncio.run(_run())


def test_contract_doc_write_tools_safety_flags() -> None:
    """含写操作的 doc 工具不得标并发安全；html convert 写文件不得标只读。"""
    from bandi.tools.doc.config_file_tool import ConfigFileTool
    from bandi.tools.doc.csv_processor import CSVProcessorTool
    from bandi.tools.doc.html_processor import HTMLProcessorTool
    from bandi.tools.doc.markdown_processor import MarkdownProcessorTool
    from bandi.tools.doc.pdf_reader import PDFReaderTool
    from bandi.tools.doc.text_processor import TextFileProcessorTool

    for cls in (
        CSVProcessorTool,
        TextFileProcessorTool,
        ConfigFileTool,
        MarkdownProcessorTool,
        HTMLProcessorTool,
        PDFReaderTool,
    ):
        assert cls.is_concurrency_safe is False, (
            f"{cls.__name__} 含写操作必须 is_concurrency_safe=False"
        )

    assert HTMLProcessorTool.is_read_only is False, "convert 写文件，不得标只读"


def test_contract_csv_negative_sort_ascending(tmp_path) -> None:
    """
    `f"{num:}"` 定宽字符串比较，-1.5 与 -2.0 数字位 '1'<'2'
    排序反（-1.5 排在 -2.0 前）。"""
    from bandi.tools.doc.csv_processor import CSVProcessorTool

    f = tmp_path / "nums.csv"
    f.write_text("group,score\ng1,-1.5\ng1,2.0\ng1,-2.0\ng1,1.0\n", encoding="utf-8")

    tool = CSVProcessorTool()
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        result = asyncio.run(
            tool.execute(
                {
                    "operation": "query",
                    "file_path": str(f),
                    "filter_column": "group",
                    "filter_value": "g1",
                    "sort_column": "score",
                    "sort_order": "asc",
                }
            )
        )
    values = [float(r["score"]) for r in result["rows"]]
    assert values == sorted(values), f"负数必须数值序，实际: {values}"


def test_contract_sensitive_name_case_insensitive() -> None:
    """
    `.GITCONFIG`/`.Env`（Windows 大小写不敏感文件系统）不得绕过拦截。"""
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    pipeline = PermissionsPipeline()
    # 直接验证检查逻辑：大写变体必须命中（6.6：单一来源 fs_tool 名单）
    for name in (".GITCONFIG", ".Env", "ID_RSA", "Credentials"):
        from bandi.tools.fs_tool import _SENSITIVE_DIRS_LOWER

        assert name.lower() in _SENSITIVE_DIRS_LOWER, f"{name}.lower() 必须在敏感名单"

    # 端到端：write 工具写 .GITCONFIG 必须被拒
    from bandi.core.message import ToolCall

    tc = ToolCall(
        id="t1",
        name="write",
        arguments={"path": "C:/Users/x/.GITCONFIG", "content": "x"},
    )
    verdict = pipeline._check_sensitive_path(tc, None)
    assert verdict.allow is False, ".GITCONFIG 大写变体必须被敏感路径拦截"


def test_contract_check_permissions_falsy_denies() -> None:
    from bandi.core.message import ToolCall
    from bandi.tools.base import BaseTool, ToolCategory
    from bandi.tools.permissions_pipeline import PermissionsPipeline

    class _BoolDenyTool(BaseTool):
        name = "bool_deny"
        description = "denies"
        category = ToolCategory.UTIL

        async def execute(self, params, context=None):  # type: ignore[override]
            return "ran"

        async def check_permissions(self, params, ctx=None):  # type: ignore[override]
            return False

    tool = _BoolDenyTool()
    pipeline = PermissionsPipeline()
    verdict = asyncio.run(
        pipeline._check_tool_permissions(  # type: ignore[arg-type]
            ToolCall(id="t1", name="bool_deny", arguments={}),
            tool,
        )
    )
    assert verdict.allow is False, "check_permissions 返回 False 必须拒绝（fail-closed）"

    class _NoneCheckTool(BaseTool):
        name = "none_check"
        description = "returns none"
        category = ToolCategory.UTIL

        async def execute(self, params, context=None):  # type: ignore[override]
            return "ran"

        async def check_permissions(self, params, ctx=None):  # type: ignore[override]
            return None

    none_tool = _NoneCheckTool()
    verdict2 = asyncio.run(
        pipeline._check_tool_permissions(  # type: ignore[arg-type]
            ToolCall(id="t2", name="none_check", arguments={}),
            none_tool,
        )
    )
    assert verdict2.allow is False, "check_permissions 返回 None 必须拒绝（fail-closed）"
    assert "no verdict" in (verdict2.reason or "").lower(), (
        f"拒绝理由应说明无判定，实际: {verdict2.reason!r}"
    )


def test_contract_shell_safety_blocks_write_forms() -> None:
    """`git branch foo`/`git tag v1` 无 flag 时不得被当只读放行（写操作）；
    粘连 `-o<path>` 输出重定向必须拦截；list-only 形态仍允许。"""
    from bandi.company.shell_safety import ShellSafetyLevel, classify_shell_command

    for cmd in ("git branch feature", "git tag v1.0", "git branch -D old"):
        verdict = classify_shell_command(cmd)
        assert verdict.level != ShellSafetyLevel.SAFE, f"{cmd} 必须非 SAFE（写操作）"
    # list-only 形态仍允许
    for cmd in ("git branch", "git branch -a", "git tag --list", "git tag -l"):
        verdict = classify_shell_command(cmd)
        assert verdict.level == ShellSafetyLevel.SAFE, f"{cmd} 应为只读列表"

    verdict = classify_shell_command("git diff -o/tmp/evil.txt")
    assert verdict.level != ShellSafetyLevel.SAFE, f"-o/tmp/evil.txt 粘连形态必须拦截: {verdict}"

    verdict = classify_shell_command("git log -o/root/x")
    assert verdict.level != ShellSafetyLevel.SAFE


def test_contract_candidate_paths_redirect_mutation_and_relative_targets() -> None:
    """`/dev/null` 重定向目标不得被当越界写目标（假阳性）；mv/cp/rm 的目标参数
    必须被提取为写候选（含相对路径以 cwd 为基准解析）。"""
    from bandi.company.collaboration_policy import _candidate_paths

    def _norm(p: Path) -> str:
        return str(p).replace("\\", "/")

    targets = _candidate_paths(
        "bash",
        {"command": "git status > /dev/null", "cwd": "."},
    )
    for path in targets:
        assert str(path).lower().endswith("dev/null") is False, (
            f"/dev/null 不得作为越界写目标，实际: {path}"
        )
    assert targets, "重定向目标应被提取（仅 /dev/null 应被排除）"

    mv_targets = _candidate_paths(
        "bash",
        {"command": "mv /etc/a.txt /outside/b.txt", "cwd": "/workspace"},
    )
    assert any(_norm(p) == "/outside/b.txt" for p in mv_targets), "mv 的目标参数必须被提取为写候选"

    cp_targets = _candidate_paths(
        "bash",
        {"command": "cp -r src /etc/evil", "cwd": "/workspace"},
    )
    assert any(_norm(p) == "/etc/evil" for p in cp_targets), "cp 的目标参数必须被提取"

    rm_targets = _candidate_paths(
        "bash",
        {"command": "rm -rf /etc/evil /var/tmp/x", "cwd": "/workspace"},
    )
    assert any(_norm(p) == "/etc/evil" for p in rm_targets), "rm 的每个目标都必须被提取"
    assert any(_norm(p) == "/var/tmp/x" for p in rm_targets)

    rel_rm = _candidate_paths("bash", {"command": "rm -rf sub/out", "cwd": "/workspace"})
    assert any(_norm(p) == "/workspace/sub/out" for p in rel_rm), "rm 相对目标必须以 cwd 为基准解析"


def test_contract_browser_runtime_isolated_per_instance() -> None:
    """
    （导航状态污染、会话交叉）。"""
    from bandi.tools.browser import BrowserRuntime

    r1 = BrowserRuntime()
    r2 = BrowserRuntime()
    assert "_instance" not in r1.__dict__ or r1._instance is None
    # 实例属性互不影响：r1 赋值不影响 r2
    r1._instance = ("pw", "browser")
    assert r2._instance is None, "实例 A 赋值不得泄漏到实例 B（类属性共享 bug）"


def test_contract_cache_hit_does_not_bypass_policy_or_permissions_pipeline() -> None:
    """DENY 的工具不得从缓存返回结果；permissions 管道路径（check_permissions
    返回 False）被缓存命中绕过同样是权限绕过面。"""
    import asyncio

    from bandi.core.message import ToolCall, ToolResult
    from bandi.core.permission.models import Decision, PermissionContext, Resource
    from bandi.tools.base import BaseTool, ToolCategory
    from bandi.tools.cache import ToolResultCache
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    class _CachedTool(BaseTool):
        name = "cached_op"
        description = "cached"
        category = ToolCategory.UTIL
        cache_ttl = 60.0

        async def execute(self, params, context=None):  # type: ignore[override]
            return "live"

    class _DenyPolicy:
        def check(self, resource: Resource, pctx: PermissionContext) -> object:
            return type("D", (), {"decision": Decision.DENY, "reason": "deny by contract"})()

    registry = ToolRegistry()
    registry.register_instance(_CachedTool())
    cache = ToolResultCache()
    executor = ToolExecutor(registry=registry, result_cache=cache, policy_engine=_DenyPolicy())
    key = cache.build_key("cached_op", {"x": 1}, scope=executor.cache_scope)
    cache.set(
        key, ToolResult(tool_call_id="c1", tool_name="cached_op", success=True, output="cached")
    )

    result = asyncio.run(
        executor.execute([ToolCall(id="c1", name="cached_op", arguments={"x": 1})])
    )[0]
    assert result.success is False, "缓存命中不得绕过 DENY 权限"
    assert "deny by contract" in result.error, f"应返回权限拒绝而非缓存结果，实际: {result.error!r}"

    class _DenyCheckTool(BaseTool):
        name = "deny_check"
        description = "denies via check_permissions"
        category = ToolCategory.UTIL
        cache_ttl = 60.0

        async def execute(self, params, context=None):  # type: ignore[override]
            return "live"

        async def check_permissions(self, params, ctx=None):  # type: ignore[override]
            return False

    registry2 = ToolRegistry()
    registry2.register_instance(_DenyCheckTool())
    cache2 = ToolResultCache()
    executor2 = ToolExecutor(registry=registry2, result_cache=cache2)
    key2 = cache2.build_key("deny_check", {"x": 1}, scope=executor2.cache_scope)
    cache2.set(
        key2, ToolResult(tool_call_id="c1", tool_name="deny_check", success=True, output="cached")
    )

    result2 = asyncio.run(
        executor2.execute([ToolCall(id="c1", name="deny_check", arguments={"x": 1})])
    )[0]
    assert result2.success is False, "缓存命中不得绕过 permissions 管道拒绝"
    assert "denied" in result2.error.lower() or "permission" in result2.error.lower(), (
        f"应返回权限拒绝而非缓存结果，实际: {result2.error!r}"
    )


def test_contract_pdf_output_sensitive_path_rejected(tmp_path) -> None:
    import fitz

    from bandi.tools.doc.pdf_reader import PDFReaderTool

    pdf = tmp_path / "src.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "contract p2-8")
        doc.save(str(pdf))

    tool = PDFReaderTool()
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path),
    ):
        result = asyncio.run(
            tool.execute(
                {
                    "operation": "merge",
                    "merge_files": [str(pdf), str(pdf)],
                    "output_path": str(tmp_path / ".git" / "evil.pdf"),
                }
            )
        )
    assert result.get("success") is False, f"merge 输出到敏感目录必须拒绝，实际: {result}"
    assert "sensitive" in str(result.get("error", "")).lower()


def test_contract_docx_read_in_thread(tmp_path, monkeypatch) -> None:
    """
    行为断言：创建真实 .docx → 调工具 → 断言内容返回正确。"""
    import asyncio

    from bandi.tools.doc.word_reader import WordReaderTool

    monkeypatch.setattr("bandi.tools.fs_tool._WORKSPACE_ROOT", tmp_path)

    docx_content = "Hello from Bandi contract test"
    f = tmp_path / "contract_test.docx"
    from docx import Document

    doc = Document()
    doc.add_paragraph(docx_content)
    doc.save(str(f))

    tool = WordReaderTool()
    result = asyncio.run(tool.execute({"file_path": str(f)}))
    assert result.get("success"), f"read failed: {result}"
    text = result.get("text", "")
    assert docx_content in text, f"expected '{docx_content}' in output, got: {text!r}"


def test_contract_gbk_encoding_detected_and_preserved(tmp_path) -> None:
    """GBK 中文必须检测为 gbk 系编码；patch 只改 ASCII 行时中文行必须原样保留。"""
    import os

    from bandi.tools.apply_patch import ApplyPatchTool
    from bandi.tools.doc.csv_processor import CSVProcessorTool

    raw = "名字,分数\n张三,95\n李四,88\n".encode("gbk")
    detected = CSVProcessorTool._detect_encoding(raw)
    assert detected.lower() in ("gbk", "gb18030", "cp936"), (
        f"GBK 中文必须检测为 gbk 系编码，实际 {detected!r}"
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "doc.txt"
    f.write_bytes("hello world\n中文注释行\n".encode("gbk"))

    tool = ApplyPatchTool()
    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("bandi.tools.fs_tool._WORKSPACE_ROOT", ws),
        patch.dict(os.environ, {"BANDI_WORKSPACE_ROOT": str(ws)}),
    ):
        result = asyncio.run(
            tool.execute(
                {
                    "patch": (
                        f"--- a/{f.name}\n+++ b/{f.name}\n@@ -1 +1 @@\n-hello world\n+hello there\n"
                    )
                }
            )
        )
    written = f.read_bytes().decode("gbk").replace("\r\n", "\n")
    assert written == "hello there\n中文注释行\n", f"GBK 中文行必须原样保留，实际内容: {written!r}"
    assert "applied" in result


def test_contract_lazy_provider_reused_and_closable() -> None:
    """
    AsyncClient 泄漏）且可显式关闭。"""
    import asyncio as aio

    from bandi.llm.mock import MockProvider
    from bandi.tools.doc import _doc_utils

    _doc_utils._lazy_provider_cache = None
    fake = MockProvider()
    with patch("bandi.agent.factory.build_provider", return_value=fake) as build_mock:
        p1 = _doc_utils.lazy_provider(None)
        p2 = _doc_utils.lazy_provider(None)
        assert p1 is p2, "第二次调用必须复用缓存实例（每次新建 = 连接池泄漏）"
        assert build_mock.call_count == 1, (
            f"build_provider 只应调用一次，实际 {build_mock.call_count}"
        )

    fake.close = AsyncMock()
    aio.run(_doc_utils.close_lazy_provider())
    assert fake.close.called, "close_lazy_provider 必须关闭缓存实例"
    assert _doc_utils._lazy_provider_cache is None, "关闭后缓存必须清空"

    # 显式传入 provider 时不缓存（工具自带实例路径不受影响）
    explicit = MockProvider()
    assert _doc_utils.lazy_provider(explicit) is explicit


# ==================== c4_company ====================
# 契约原则：claimed 去重配额 / peer_wait 恢复清理 / hook extra 填充 /
# refresh 转换进 hook / CLI 异常不吞噬 / heartbeat 钳制 + naive 防御 /


def _dispatcher(store: WorkItemStore, max_concurrent: int = 3) -> CompanyDispatcher:
    org = OrgEngine()
    org.register_role("worker", "Worker", "Executes tasks")
    org.register_role("manager", "Manager", "Reviews", reports_to="worker")
    recruiter = Recruiter()
    recruiter.register_seat("worker")
    recruiter.register_seat("manager")
    return CompanyDispatcher(
        store=store,
        recruiter=recruiter,
        org_engine=org,
        gate_harness=GateHarness(),
        config=DispatcherConfig(max_concurrent=max_concurrent),
    )


def test_contract_claimed_dedup_by_role(tmp_path) -> None:
    """
    不得重复计数（否则配额虚增，todo 分支饿死）。"""
    store = WorkItemStore(str(tmp_path / "c.db"))
    disp = _dispatcher(store, max_concurrent=2)
    for i in range(4):
        item = create_work_item(
            title=f"t{i}", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker"
        )
        store.save(item)
    report = asyncio.run(disp.tick())
    # 4 张卡同一角色——claimed 去重后最多 1 个 worker 条目
    assert report.claimed.count("worker") <= 1, f"claimed 必须按角色去重: {report.claimed}"


def test_contract_recover_clears_peer_wait_only_for_crashed_trace(tmp_path) -> None:
    """行为族：recover 只处理「崩溃残留」（有执行痕迹+租约过期）。
    崩溃残留：转 READY 且清 peer_wait（防 resolve_task_peer_wait 在 READY 卡上
    再次触发重复升级/死锁）；合法等待（无执行痕迹）：不得触碰
    （phase 与 peer_wait 均保留，等待由 peer/heartbeat 链推进）。"""
    from datetime import UTC, datetime, timedelta

    store = WorkItemStore(str(tmp_path / "c.db"))
    disp = _dispatcher(store)

    legal = create_work_item(
        title="legal wait",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.WAITING_FOR_PEER,
        assignee_role="worker",
        metadata={"peer_wait": {"waiting_on_agents": ["manager"]}},
    )
    store.save(legal)
    assert disp.recover_stale_claims() == 0, "合法等待不得被恢复"
    loaded = store.get(legal.id)
    assert loaded is not None and loaded.phase == Phase.WAITING_FOR_PEER
    assert loaded.metadata.get("peer_wait"), "合法等待的 peer_wait 必须保留"

    expired = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    crashed = create_work_item(
        title="crashed wait",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.WAITING_FOR_PEER,
        assignee_role="worker",
        metadata={
            "peer_wait": {"waiting_on_agents": ["manager"]},
            "execution_claim": {"instance_id": "dead-proc", "lease_expires_at": expired},
        },
    )
    store.save(crashed)
    assert disp.recover_stale_claims() == 1, "过期租约的崩溃残留必须恢复"
    loaded2 = store.get(crashed.id)
    assert loaded2 is not None and loaded2.phase == Phase.READY
    assert "peer_wait" not in (loaded2.metadata or {}), "崩溃残留的 peer_wait 必须被清理"


def test_contract_dispatcher_tick_hooks_extra_and_settlement(tmp_path) -> None:
    """tick 触发的 hook 上下文必须填充 session_id/role_key（enqueue_session_work_on_runnable_hook）；
    refresh_dependents 的 settlement 转换必须收集进 report.transitions 并被 hook 消费。"""
    from bandi.company.phase_hooks import HookContext, HookRegistry
    from bandi.company.work_item_transition import transition_work_item

    captured: list[HookContext] = []
    registry = HookRegistry()
    registry.register("dispatcher_tick", lambda ctx: captured.append(ctx))

    store = WorkItemStore(str(tmp_path / "c.db"))
    disp = _dispatcher(store)
    disp.hook_registry = registry
    item = create_work_item(
        title="with session",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.READY,
        assignee_role="worker",
        metadata={"session_id": "sess-1"},
    )
    store.save(item)
    asyncio.run(disp.tick())
    assert captured, "hook 必须被触发"
    first_ctx = captured[0]
    assert first_ctx.extra is not None and first_ctx.extra.get("session_id") == "sess-1", (
        f"extra 必须含 session_id，实际: {first_ctx.extra}"
    )
    assert first_ctx.extra.get("role_key") == "worker", f"extra 必须含 role_key: {first_ctx.extra}"

    store2 = WorkItemStore(str(tmp_path / "c2.db"))
    disp2 = _dispatcher(store2)
    captured2: list[HookContext] = []
    registry2 = HookRegistry()
    registry2.register("dispatcher_tick", lambda ctx: captured2.append(ctx))
    disp2.hook_registry = registry2

    child = create_work_item(title="child", turn_type=WorkItemTurnType.EXECUTE)
    store2.save(child)
    transition_work_item(store2.get(child.id), Phase.FAILED, store2, reason="seed")
    parent = create_work_item(
        title="parent",
        turn_type=WorkItemTurnType.AGGREGATE,
        metadata={"dependency_work_item_ids": [child.id]},
    )
    store2.save(parent)

    asyncio.run(disp2.tick())

    assert captured2, "settlement 兜底必须触发 hook（转换必须被收集）"
    involved = {c.work_item_id for c in captured2}
    assert parent.id in involved, f"settlement 转换必须被 hook 感知，实际 hook 到: {involved}"


def test_contract_role_responsibility_kept_across_serialization() -> None:
    """角色职责必须可读回（进 work item summary / 上下文渲染）；
    序列化往返——_role_to_dict/_role_from_dict 及 set_reports_to 重建 OrgRole
    均不得丢职责（org.json 重载后 work item summary 丢角色职责）。"""
    from bandi.company.org_engine import OrgEngine

    org = OrgEngine()
    org.register_role("designer", "Designer", "owns UI design")
    role = org.get_role("designer")
    assert role is not None and role.responsibility == "owns UI design", (
        f"responsibility 必须保留，实际: {role.responsibility if role else None}"
    )

    org.register_role("lead", "Lead", "owns team")

    payload = org._role_to_dict(org.get_role("designer"))  # type: ignore[arg-type]
    restored = OrgEngine._role_from_dict(payload)
    assert restored.responsibility == "owns UI design", (
        f"序列化往返必须保留 responsibility，实际: {restored.responsibility!r}"
    )

    org.set_reports_to("designer", "lead")
    role2 = org.get_role("designer")
    assert role2 is not None and role2.responsibility == "owns UI design", (
        f"set_reports_to 后 responsibility 必须保留，实际: {role2.responsibility if role2 else None}"
    )


def test_contract_gate_stagnation_escalates_at_max(tmp_path) -> None:
    gate = GateHarness(max_reworks=3)
    for i in range(3):
        r = gate.evaluate("wi1", "requirements unchanged", "")
        if i < 2:
            assert r.verdict == GateVerdict.REWORK, f"第 {i + 1} 次应 REWORK，实际 {r.verdict}"
        else:
            assert r.verdict == GateVerdict.ESCALATE, (
                f"第 3 次空输出必须 ESCALATE，实际 {r.verdict}"
            )


def test_contract_heartbeat_clamps_and_tolerates_naive_datetime(tmp_path) -> None:
    """0 并发必须钳制为 1；naive updated_at 不得使停滞检测崩溃（`now - naive` 抛 TypeError）。"""
    from bandi.company.heartbeat import HeartbeatScheduler

    store = WorkItemStore(str(tmp_path / "c.db"))
    org = OrgEngine()
    disp = _dispatcher(store)
    s = HeartbeatScheduler(
        store=store,
        org_engine=org,
        dispatcher=disp,
        max_concurrent_runs=0,
    )
    assert s.max_concurrent_runs == 1, "0 必须钳制为 1（至少允许 1 个并发 run）"

    store2 = WorkItemStore(str(tmp_path / "c2.db"))
    disp2 = _dispatcher(store2)
    s2 = HeartbeatScheduler(
        store=store2,
        org_engine=org,
        dispatcher=disp2,
        stale_waits_minutes=30,
    )
    item = create_work_item(
        title="naive wait",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.WAITING_FOR_PEER,
        assignee_role="worker",
    )
    item.updated_at = datetime(2026, 1, 1, 12, 0, 0)  # naive（无 tzinfo）
    store2.save(item)
    asyncio.run(s2._resolve_stale_waits())
    assert s2.last_stale_report, "naive updated_at 不得使停滞检测崩溃"


def test_contract_chat_controller_error_not_swallowed() -> None:
    from bandi.cli.chat_controller import ChatTurnController, QueuedChatInput

    controller = ChatTurnController()
    errors: list[Exception] = []

    async def _failing(item: QueuedChatInput) -> None:
        raise RuntimeError("boom")

    controller.set_executor(_failing)
    controller.set_on_error(lambda item, exc: errors.append(exc))

    async def _run() -> None:
        controller.submit(QueuedChatInput(text="hi"))
        await asyncio.sleep(0.1)

    asyncio.run(_run())
    assert errors, "异常必须走错误回调（不得静默吞噬）"
    assert not controller.is_busy, "异常后队列必须恢复可用"


def test_contract_run_agent_turn_dimension_isolation(tmp_path) -> None:
    """
    hierarchy 必须构建且记忆按维度隔离——不传维度时 hierarchy 不构建
    （维度传参生产接线验证）。"""
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.message import LLMResponse, TokenUsage
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore

    with patch("pathlib.Path.home", return_value=tmp_path):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content="done",
                usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            )
        )
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            result = asyncio.run(
                run_agent_turn(
                    user_input="hello",
                    session_id=sid,
                    session_store=store,
                    project_id="proj-a",
                    employee_id="emp-1",
                )
            )
        assert result.reply, "维度传参后必须正常执行"

        # hierarchy 必须构建（维度传参激活记忆层级管理器——不传维度则无 DB）
        hierarchy_db = tmp_path / ".bandi" / "data" / "hierarchy_global.db"
        assert hierarchy_db.exists(), "传 project_id+employee_id 后 hierarchy 必须构建"
        store.close()


def test_contract_dispatcher_default_wiring(tmp_path) -> None:
    """
    装配 TaskPreparer/ApprovalEngine/Heartbeat——三处生产装配点（CLI/API/
    custom_runtime）未传参时 reorg 审批不得抛 RuntimeError、任务预处理钩子
    与心跳调度器必须可达。"""
    from bandi.company.approval import ApprovalEngine
    from bandi.company.task_preparer import TaskPreparer

    store = WorkItemStore(str(tmp_path / "c.db"))
    disp = _dispatcher(store)
    assert isinstance(disp.approval_engine, ApprovalEngine), "approval_engine 必须默认装配"
    assert isinstance(disp.task_preparer, TaskPreparer), "task_preparer 必须默认装配"
    assert disp._build_heartbeat() is not None, "heartbeat 必须可构建"
    store.close()


def test_contract_reach_consensus_llm_failure_closes(tmp_path) -> None:
    from bandi.company.communication import CommunicationManager

    comm = CommunicationManager(workspace=tmp_path, project_id="p", session_id="s")
    room_id = comm.create_room(["a", "b"], agenda="decision")
    assert comm.post_to_room(room_id, "a", "我们赞成方案 X"), "room 消息必须可追加"

    async def _boom(prompt: str) -> str:
        raise RuntimeError("llm down")

    agreed, text = asyncio.run(comm.reach_consensus(room_id, "decision", llm=_boom))
    assert agreed is False, "LLM 异常时必须以未达成共识返回（fail-closed）"
    assert "NO_CONSENSUS" in text.upper(), f"结果应标注未达成共识: {text}"


def test_contract_store_close_releases_connections_idempotently(tmp_path) -> None:
    """WorkItemStore 关闭后仍可再读确认，重复 close 不抛错；
    MemoryHierarchyManager 持有 GlobalMemoryStore/WorkItemStore SQLite 连接，
    关闭后不抛错且幂等。"""
    store = WorkItemStore(str(tmp_path / "c.db"))
    store.save(
        create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE, assignee_role="worker")
    )
    assert store.get("t") is None  # id 非 "t"，仅确认可读
    store.close()
    # 幂等：重复 close 不抛错
    store.close()

    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager

    manager = MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(tmp_path / "g.db")),
        markdown_memory=MarkdownMemoryStore(tmp_path),
        work_item_store=WorkItemStore(str(tmp_path / "w.db")),
        store_dir=tmp_path,
    )
    manager.close()
    manager.close()  # 幂等：重复 close 不抛错


def test_contract_reorg_event_unknown_kind_defaults() -> None:
    """
    回退默认（脏数据不崩反序列化链）——同族变体。"""
    from bandi.company.reorg_types import ReorgEventKind, ReorgEventRecord

    rec = ReorgEventRecord.from_dict({"event_kind": "bogus_kind"})
    assert rec.event_kind == ReorgEventKind.PROPOSED, (
        f"未知枚举必须回退 PROPOSED，实际 {rec.event_kind!r}"
    )


def test_contract_fairness_word_boundary(tmp_path) -> None:
    """
    `term in text_lower` 子串匹配，"man" 命中 "manager" 误判。"""
    from bandi.eval.fairness_evaluator import FairnessEvaluator

    ev = FairnessEvaluator()
    report = ev.evaluate("The manager approved the human resources plan.")
    terms = {i.term for i in report.issues}
    assert "man" not in terms, f"'man' 不得匹配 'manager'/'human' 子串，实际 {terms}"
