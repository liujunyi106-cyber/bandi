from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# 功能：契约——审计链主体维度（actor）+ 追加 fsync 与失败回滚（.4）
#
# 且 append 无 fsync —— 崩溃时尾部可能只落半行，链校验会直接判 invalid JSON
# deepseek-harness `session-persistence-jsonl` appendLines（写后 sync，失败截断回滚）。


class _StubStore:
    """record_event 不触达 session store——审计链写路径的测试无需真库。"""

    def list_sessions(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


def _reporter(tmp_path: Path) -> Any:
    from bandi.observability.audit_reporter import AuditReporter

    return AuditReporter(_StubStore(), chain_path=tmp_path / "chain.jsonl")  # type: ignore[arg-type]


def _entries(tmp_path: Path) -> list[dict]:
    path = tmp_path / "chain.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_actor_recorded_default_explicit_derived_and_invalid_kind(tmp_path: Path) -> None:
    """契约：默认/显式/上下文派生主体均正确落盘，非法主体类型显式报错。"""
    from bandi.observability.audit_reporter import AuditActor
    from bandi.observability.context import bind_log_context

    default_dir = tmp_path / "default"
    default_dir.mkdir()
    reporter = _reporter(default_dir)
    reporter.record_event({"action": "t"})

    entry = _entries(default_dir)[0]
    assert entry["actor"] == {"kind": "system", "id": "local"}, f"主体缺失：{entry.get('actor')}"

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    reporter = _reporter(explicit_dir)
    reporter.record_event({"action": "t"}, actor=AuditActor(kind="auto_review", id="reviewer-1"))

    entry = _entries(explicit_dir)[0]
    assert entry["actor"] == {"kind": "auto_review", "id": "reviewer-1"}

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    reporter = _reporter(context_dir)
    with bind_log_context(agent_id="emp-7", session_id="s-3", request_id="req-9"):
        reporter.record_event({"action": "t"})

    entry = _entries(context_dir)[0]
    assert entry["actor"] == {"kind": "agent", "id": "emp-7"}, f"未从上下文派生主体：{entry}"
    assert entry["session_id"] == "s-3", "审计条目未携带会话标识"
    assert entry["request_id"] == "req-9", "审计条目未携带请求标识"

    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    reporter = _reporter(invalid_dir)
    with pytest.raises(ValueError, match="非法 audit actor kind"):
        reporter.record_event({"action": "t"}, actor=AuditActor(kind="hacker", id="x"))


def test_chain_verifies_detects_tamper_and_accepts_legacy(tmp_path: Path) -> None:
    """回归契约：加了主体字段后链校验仍通过，篡改可检出，历史链保持可校验。"""
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir()
    reporter = _reporter(verify_dir)
    for i in range(5):
        reporter.record_event({"action": "t", "seq": i})
    assert reporter.verify() == []

    tamper_dir = tmp_path / "tamper"
    tamper_dir.mkdir()
    reporter = _reporter(tamper_dir)
    reporter.record_event({"action": "t"})
    reporter.record_event({"action": "t2"})

    path = tamper_dir / "chain.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    last = json.loads(lines[-1])
    last["actor"] = {"kind": "user", "id": "forged"}
    path.write_text("\n".join(lines[:-1] + [json.dumps(last, ensure_ascii=False)]) + "\n", "utf-8")

    issues = reporter.verify()
    assert any(i["error"] == "hash mismatch" for i in issues), "篡改 actor 未被检出"

    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    chain = legacy_dir / "chain.jsonl"
    legacy = {
        "timestamp": 1.0,
        "event": {"action": "old"},
        "prev_hash": "",
    }
    raw = json.dumps(legacy, sort_keys=True, ensure_ascii=False)
    import hashlib

    legacy["entry_hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    chain.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

    reporter = _reporter(legacy_dir)
    assert reporter.verify() == [], "历史链因新增字段而校验失败——破坏了向后兼容"


def test_fsync_failure_rolls_back_partial_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """破坏者契约：append 中途失败必须回滚到原长度，不得留下半行污染审计链。"""
    from bandi.observability import audit_reporter as mod

    reporter = _reporter(tmp_path)
    reporter.record_event({"action": "first"})
    chain = tmp_path / "chain.jsonl"
    size_before = chain.stat().st_size

    real_fsync = os.fsync
    calls = {"n": 0}

    def flaky_fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", flaky_fsync)

    with pytest.raises(OSError):
        reporter.record_event({"action": "second"})

    assert calls["n"] >= 1, "未走到 fsync——测试前提不成立"
    assert chain.stat().st_size == size_before, (
        f"写失败后未回滚：{chain.stat().st_size} != {size_before}（可能留下半行）"
    )

    monkeypatch.undo()
    assert reporter.verify() == [], "回滚后审计链应完好可校验"


# ==================== t1_cost_store ====================
# 功能：契约——LLM 成本台账落库（.2）
#
# 「上个会话花了多少 / 哪个员工最贵」，前端展示用量无数据源。


def r1xt1_tracker(tmp_path: Path, *, store: Any = None) -> Any:
    from bandi.llm.cost_tracker import CostTracker
    from bandi.llm.model_catalog import ModelCatalog

    return CostTracker(catalog=ModelCatalog(), budget_limit=-1.0, store=store)


def _store(tmp_path: Path, **kw: Any) -> Any:
    from bandi.observability.cost_store import CostStore

    return CostStore(tmp_path / "cost.db", **kw)


def test_record_persists_full_row_and_duplicate_event_id_is_idempotent(tmp_path: Path) -> None:
    """契约：一次 LLM 调用落一条完整台账；同一次调用的重复入库被唯一索引拦下。"""
    from bandi.observability.cost_store import CostRecord

    full_dir = tmp_path / "full"
    full_dir.mkdir()
    store = _store(full_dir)
    tracker = r1xt1_tracker(full_dir, store=store)
    try:
        tracker.record(
            session_id="s-1",
            provider="bandi",
            model="deepseek-v4-flash",
            prompt_tokens=1200,
            completion_tokens=300,
            breakdown={"agent_role": "MEMBER", "turn_id": 4, "trace_id": "t-1"},
        )
        store.flush()
        rows = store.list_records()
    finally:
        store.close()

    assert len(rows) == 1, f"应落 1 条，实际 {len(rows)}"
    row = rows[0]
    assert row["session_id"] == "s-1"
    assert row["provider_name"] == "bandi"
    assert row["model"] == "deepseek-v4-flash"
    assert row["input_tokens"] == 1200
    assert row["output_tokens"] == 300
    assert row["agent_role"] == "MEMBER"
    assert row["turn_id"] == 4
    assert row["trace_id"] == "t-1"
    assert row["total_cost_cny"] > 0, "费用未计价"
    assert row["call_kind"] == "llm"

    dup_dir = tmp_path / "dup"
    dup_dir.mkdir()
    store = _store(dup_dir)
    try:
        rec = CostRecord(
            session_id="s-1",
            model="m",
            input_tokens=10,
            output_tokens=5,
            total_cost_cny=0.01,
            event_id="fixed-id",
        )
        store.enqueue(rec)
        store.enqueue(rec)
        store.flush()
        rows = store.list_records()
        stats = store.stats()
    finally:
        store.close()

    assert len(rows) == 1, f"重复 event_id 未去重：{len(rows)} 条"
    assert stats.duplicated >= 1, "未计入去重计数"


def r1xt1_test_queue_saturation_does_not_block(tmp_path: Path) -> None:
    """契约：队满只丢弃并计数，不得阻塞 LLM 主链。"""
    import time

    from bandi.observability.cost_store import CostRecord

    store = _store(tmp_path, maxsize=4)
    try:
        started = time.monotonic()
        for i in range(3000):
            store.enqueue(CostRecord(session_id="s", model="m", event_id=f"e-{i}"))
        elapsed = time.monotonic() - started
    finally:
        store.close()

    assert elapsed < 5.0, f"入队 3000 条耗时 {elapsed:.2f}s —— 疑似阻塞"
    assert store.stats().enqueued + store.stats().dropped == 3000, "条目既未入队也未计入丢弃"


def test_persistence_failure_does_not_break_billing(tmp_path: Path) -> None:
    """破坏者契约：落库抛异常时，`record` 必须照常返回并完成内存计费。"""

    class _BoomStore:
        def enqueue(self, record: Any) -> None:
            raise OSError("disk gone")

    tracker = r1xt1_tracker(tmp_path, store=_BoomStore())
    entry = tracker.record(
        session_id="s-1",
        provider="p",
        model="deepseek-v4-flash",
        prompt_tokens=10,
        completion_tokens=5,
    )

    assert entry is not None, "落库失败却影响了返回值"
    assert tracker.daily_total > 0, "落库失败却影响了内存计费"


def test_close_flushes_pending_records_and_query_survives_restart(tmp_path: Path) -> None:
    """契约：优雅关闭排空在途条目；台账跨进程可查（重启不归零）。"""
    flush_dir = tmp_path / "flush"
    flush_dir.mkdir()
    store = _store(flush_dir)
    tracker = r1xt1_tracker(flush_dir, store=store)
    for i in range(50):
        tracker.record(
            session_id=f"s-{i}",
            provider="p",
            model="deepseek-v4-flash",
            prompt_tokens=100,
            completion_tokens=50,
        )
    store.close()  # 不显式 flush，直接关

    reopened = _store(flush_dir)
    try:
        rows = reopened.list_records(limit=200)
    finally:
        reopened.close()
    assert len(rows) == 50, f"关闭时未排空，仅落 {len(rows)}/50 条"

    restart_dir = tmp_path / "restart"
    restart_dir.mkdir()
    store = _store(restart_dir)
    tracker = r1xt1_tracker(restart_dir, store=store)
    tracker.record(
        session_id="sess-x",
        provider="p",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=500,
    )
    store.close()

    reopened = _store(restart_dir)
    try:
        summary = reopened.session_cost("sess-x")
    finally:
        reopened.close()

    assert summary["calls"] == 1
    assert summary["input_tokens"] == 1000
    assert summary["output_tokens"] == 500
    assert summary["total_cost_cny"] > 0


def test_cost_by_role_aggregation(tmp_path: Path) -> None:
    """契约：按角色聚合是「哪个 AI 员工最贵」的唯一数据源。"""
    store = _store(tmp_path)
    tracker = r1xt1_tracker(tmp_path, store=store)
    tracker.record(
        session_id="s1",
        provider="p",
        model="deepseek-v4-flash",
        prompt_tokens=1000,
        completion_tokens=1000,
        breakdown={"agent_role": "ENGINEER"},
    )
    tracker.record(
        session_id="s2",
        provider="p",
        model="deepseek-v4-flash",
        prompt_tokens=10,
        completion_tokens=10,
        breakdown={"agent_role": "REVIEWER"},
    )
    store.flush()
    try:
        by_role = store.cost_by_role()
    finally:
        store.close()

    assert "ENGINEER" in by_role and "REVIEWER" in by_role
    assert by_role["ENGINEER"]["cost"] > by_role["REVIEWER"]["cost"]
    assert by_role["ENGINEER"]["tokens"] == 2000


def test_shared_store_is_keyed_by_path(tmp_path: Path) -> None:
    """破坏者契约：不同路径必须拿到不同实例（防 同族的路径串扰）。"""
    from bandi.observability.cost_store import get_shared_cost_store, reset_shared_cost_stores

    reset_shared_cost_stores()
    a = get_shared_cost_store(tmp_path / "a.db")
    b = get_shared_cost_store(tmp_path / "b.db")
    a_again = get_shared_cost_store(tmp_path / "a.db")
    try:
        assert a is not b, "不同路径返回了同一实例"
        assert a is a_again, "同路径未复用实例（会重复开连接）"
    finally:
        reset_shared_cost_stores()


def test_wiring_agent_runner_passes_cost_store() -> None:
    """接线契约：装配点必须真的把台账交给共享 tracker，否则落库永不生效。"""
    pkg = Path(__file__).resolve().parents[2] / "bandi"
    src = (pkg / "core" / "agent_runner.py").read_text(encoding="utf-8")
    assert "get_shared_cost_store(" in src, "agent_runner 未创建成本台账"
    assert "store=cost_store" in src, "成本台账未传给 get_shared_cost_tracker"


@pytest.mark.parametrize("bad_store", [None])
def test_tracker_without_store_is_pure_memory(tmp_path: Path, bad_store: Any) -> None:
    """兼容契约：不传 store 时行为与修复前完全一致（默认零副作用）。"""
    tracker = r1xt1_tracker(tmp_path, store=bad_store)
    tracker.record(
        session_id="s",
        provider="p",
        model="deepseek-v4-flash",
        prompt_tokens=5,
        completion_tokens=5,
    )
    assert tracker.daily_total > 0
    assert len(tracker.entries) == 1


# ==================== t1_log_rotation ====================
# 功能：契约——日志双触发轮转（按天 + 按大小）与保留上限（.3）
#
# 背景：`observability/logger.py` 原 `_rotate_if_needed` 只比对 `st_mtime` 的日期
# **零大小轮转**——单次高频会话可让 bandi.log 无上限增长。


def r2xt1_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _archives(log_dir: Path) -> list[Path]:
    return sorted(p for p in log_dir.glob("bandi-*.log"))


def test_size_rotation_backup_cap_and_no_overwrite(tmp_path: Path) -> None:
    """契约：超过 max_bytes 切档重建；保留档数受 backup_count 限制；同日多次轮转不互相覆盖。"""
    from bandi.observability.logger import Logger

    size_dir = tmp_path / "size"
    size_dir.mkdir()
    logger = Logger(size_dir, console_output=False, max_bytes=2048)
    try:
        for i in range(200):
            logger.info("mod", f"payload-{i}-{'x' * 100}")
    finally:
        logger.close()

    archives = _archives(size_dir)
    assert archives, "超过 max_bytes 却未产生轮转档"
    assert (size_dir / "bandi.log").exists(), "轮转后当前文件必须重建"

    cap_dir = tmp_path / "cap"
    cap_dir.mkdir()
    logger = Logger(cap_dir, console_output=False, max_bytes=1024, backup_count=3)
    try:
        for i in range(500):
            logger.info("mod", f"payload-{i}-{'y' * 120}")
    finally:
        logger.close()

    archives = _archives(cap_dir)
    assert len(archives) <= 3, f"保留档超上限：{len(archives)} 份 -> {[p.name for p in archives]}"

    same_day_dir = tmp_path / "same_day"
    same_day_dir.mkdir()
    logger = Logger(same_day_dir, console_output=False, max_bytes=1024, backup_count=50)
    try:
        for i in range(400):
            logger.info("mod", f"msg-{i}-{'z' * 150}")
    finally:
        logger.close()

    archives = _archives(same_day_dir)
    assert len(archives) > 1, "同一天应产生多份轮转档（否则发生了覆盖）"
    names = {p.name for p in archives}
    assert len(names) == len(archives), f"轮转档命名冲突：{names}"

    all_msgs = {e["message"] for p in archives for e in r2xt1_entries(p)}
    assert len(all_msgs) > 100, f"多份档内容疑似被互相覆盖，仅剩 {len(all_msgs)} 条"


def test_day_rotation_and_utf8_preserved(tmp_path: Path) -> None:
    """回归契约：跨天轮转保留（不被大小逻辑顶掉）；中文/表情跨轮转读取无损。"""
    from bandi.observability.logger import Logger

    day_dir = tmp_path / "day"
    day_dir.mkdir()
    logger = Logger(day_dir, console_output=False, max_bytes=10**9)
    logger.info("mod", "day-old")
    logger.close()

    current = day_dir / "bandi.log"
    assert current.exists()
    old = time.time() - 2 * 86400
    os.utime(current, (old, old))

    logger2 = Logger(day_dir, console_output=False, max_bytes=10**9)
    try:
        logger2.info("mod", "after-roll")
    finally:
        logger2.close()

    archives = _archives(day_dir)
    assert archives, "跨天未轮转——按天触发被大小逻辑顶掉了"
    assert any(e["message"] == "day-old" for p in archives for e in r2xt1_entries(p)), (
        "旧内容未进入轮转档"
    )

    utf8_dir = tmp_path / "utf8"
    utf8_dir.mkdir()
    logger = Logger(utf8_dir, console_output=False, max_bytes=512, backup_count=10)
    payload = "中文消息·表情🙂·符号→"
    try:
        for i in range(60):
            logger.info("mod", f"{payload}-{i}")
    finally:
        logger.close()

    texts = [e["message"] for p in _archives(utf8_dir) for e in r2xt1_entries(p)]
    texts += [e["message"] for e in r2xt1_entries(utf8_dir / "bandi.log")]
    assert any(payload in t for t in texts), "轮转后中文内容丢失或乱码"


def test_existing_signature_still_compatible(tmp_path: Path) -> None:
    """兼容契约：不传新参数时行为与旧版一致（默认 10MB / 5 份），既有调用点零改动。"""
    from bandi.observability.logger import Logger

    logger = Logger(tmp_path, console_output=False)
    try:
        logger.info("mod", "compat")
    finally:
        logger.close()

    assert logger._max_bytes == 10 * 1024 * 1024
    assert logger._backup_count == 5


# ==================== t1_logger_mainchain ====================
# 功能：契约——自研 JSON Logger 接入主链（.1）
#
# 背景：`bandi/observability/logger.py` 的 Logger 实现完整（JSONL + 脱敏 + trace_id）
# 但全仓只在 `agent_runner.py:982` 实例化、只订阅 ERROR_OCCURRED / DEGRADATION 两个事件
# （`event_bus.py:479-480`），主循环与状态机全走 stdlib logging → **结构化日志未覆盖主链**。
#
# 本契约钉死 stdlib→Logger 桥的四条硬约束：
# 1. 转发后条目字段完整（level/module/message + 上下文字段）；
# 2. 上下文按 asyncio task 隔离，并发会话不串（破坏者）；
# 3. 队列饱和时**绝不阻塞主链**，只丢弃并计数；
# 4. Logger 写入异常不得穿透到业务代码（破坏者）。


def _read_entries(log_dir: Path) -> list[dict]:
    path = log_dir / "bandi.log"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_bridge_writes_jsonl_and_injects_context_fields(tmp_path: Path) -> None:
    """契约：装桥后 stdlib 日志落 JSON 条目；当前日志上下文被注入条目字段。"""
    from bandi.observability.context import bind_log_context
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge

    write_dir = tmp_path / "write"
    write_dir.mkdir()
    logger = Logger(write_dir, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test1")
    try:
        logging.getLogger("bandi.test1.sub").info("hello mainchain")
    finally:
        handler.close()
        logger.close()

    entries = _read_entries(write_dir)
    assert len(entries) == 1, f"应恰好落 1 条，实际 {len(entries)}"
    entry = entries[0]
    assert entry["level"] == "INFO"
    assert entry["message"] == "hello mainchain"
    assert "test1" in entry["module"], f"module 应取自 logger 名：{entry['module']}"

    ctx_dir = tmp_path / "ctx"
    ctx_dir.mkdir()
    logger = Logger(ctx_dir, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test2")
    try:
        with bind_log_context(
            session_id="s-1",
            turn_index=7,
            agent_id="a-1",
            role="MEMBER",
            run_id="r-1",
            request_id="req-1",
        ):
            logging.getLogger("bandi.test2").warning("with ctx")
    finally:
        handler.close()
        logger.close()

    entry = _read_entries(ctx_dir)[0]
    assert entry["session_id"] == "s-1"
    assert entry["turn_index"] == 7
    assert entry["agent_id"] == "a-1"
    assert entry["role"] == "MEMBER"
    assert entry["run_id"] == "r-1"
    assert entry["request_id"] == "req-1"


def test_context_not_leaked_outside_binding(tmp_path: Path) -> None:
    """破坏者契约：bind 退出后上下文必须复原，不得泄漏到后续日志。"""
    from bandi.observability.context import bind_log_context, current_log_context
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge

    logger = Logger(tmp_path, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test3")
    try:
        with bind_log_context(session_id="leak-me"):
            pass
        assert current_log_context().session_id == "", "上下文泄漏出 with 块"
        logging.getLogger("bandi.test3").info("after")
    finally:
        handler.close()
        logger.close()

    assert _read_entries(tmp_path)[0]["session_id"] == ""


@pytest.mark.asyncio
async def test_context_isolated_between_concurrent_tasks(tmp_path: Path) -> None:
    """破坏者契约：两个并发 task 各自 bind，日志条目不得串 session。"""
    from bandi.observability.context import bind_log_context
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge

    logger = Logger(tmp_path, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test4")

    async def _worker(sid: str) -> None:
        with bind_log_context(session_id=sid):
            await asyncio.sleep(0)
            logging.getLogger("bandi.test4").info(f"from {sid}")
            await asyncio.sleep(0)

    try:
        await asyncio.gather(_worker("alpha"), _worker("beta"))
    finally:
        handler.close()
        logger.close()

    entries = _read_entries(tmp_path)
    assert len(entries) == 2, f"应 2 条，实际 {len(entries)}"
    mapping = {e["message"]: e["session_id"] for e in entries}
    assert mapping["from alpha"] == "alpha", f"并发串号：{mapping}"
    assert mapping["from beta"] == "beta", f"并发串号：{mapping}"


def r3xt1_test_queue_saturation_does_not_block(tmp_path: Path) -> None:
    """契约：队列打满时 emit 只丢弃 + 计数，**绝不阻塞调用方**（主链安全）。"""
    import time

    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge

    logger = Logger(tmp_path, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test5", maxsize=8)
    log = logging.getLogger("bandi.test5")
    total = 3000
    try:
        started = time.monotonic()
        for i in range(total):
            log.info("flood %d", i)
        elapsed = time.monotonic() - started
    finally:
        handler.close()
        logger.close()

    assert elapsed < 5.0, f"灌 {total} 条耗时 {elapsed:.2f}s —— 疑似阻塞"
    stats = handler.stats()
    assert stats.dropped > 0, "maxsize=8 灌 3000 条却零丢弃，说明并未真正用有界队列"
    assert stats.dropped + stats.written == total, (
        f"条目既未写入也未计入丢弃：dropped={stats.dropped} written={stats.written} total={total}"
    )


def test_bridge_survives_broken_logger(tmp_path: Path) -> None:
    """破坏者契约：Logger 底层写入抛异常时，不得穿透到业务代码。"""
    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import install_logger_bridge

    logger = Logger(tmp_path, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test6")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk gone")

    logger._file.write = _boom  # type: ignore[method-assign]
    try:
        logging.getLogger("bandi.test6").error("should not raise")
    finally:
        handler.close()
        logger.close()
    # 走到这里没抛异常即通过


def test_install_idempotent_and_uninstall_stops_forwarding(tmp_path: Path) -> None:
    """契约：重复安装返回同一 handler 且不重复写；卸载后不再转发且从 handlers 移除。"""
    import logging as _logging

    from bandi.observability.logger import Logger
    from bandi.observability.stdlib_bridge import (
        install_logger_bridge,
        uninstall_logger_bridge,
    )

    idem_dir = tmp_path / "idem"
    idem_dir.mkdir()
    logger = Logger(idem_dir, console_output=False)
    h1 = install_logger_bridge(logger, logger_name="bandi.test7")
    h2 = install_logger_bridge(logger, logger_name="bandi.test7")
    try:
        logging.getLogger("bandi.test7").info("once")
    finally:
        h2.close()
        logger.close()

    entries = _read_entries(idem_dir)
    assert len(entries) == 1, f"重复安装导致重复条目：{len(entries)} 条"
    assert h1 is h2, "幂等安装应返回同一 handler"

    uninstall_dir = tmp_path / "uninstall"
    uninstall_dir.mkdir()
    logger = Logger(uninstall_dir, console_output=False)
    handler = install_logger_bridge(logger, logger_name="bandi.test8")
    uninstall_logger_bridge(handler)
    try:
        logging.getLogger("bandi.test8").info("dropped")
    finally:
        handler.close()
        logger.close()

    assert _read_entries(uninstall_dir) == [], "卸载后仍在转发"
    assert handler not in _logging.getLogger("bandi.test8").handlers


# ---------------------------------------------------------------- 接线契约
# 模块自测通过 ≠ 生产用上了。以下三条钉住「桥与上下文确实进了主链」。

R3XT1_PKG_ROOT = Path(__file__).resolve().parents[2] / "bandi"


def test_wiring_agent_runner_installs_bridge_and_loop_binds_log_context() -> None:
    """接线契约：`run_agent_turn` 真装桥且 logger 注入 CoreDeps；主循环绑定 run 级上下文 + 逐轮刷 turn。"""
    src = (R3XT1_PKG_ROOT / "core" / "agent_runner.py").read_text(encoding="utf-8")
    assert "install_logger_bridge(" in src, "agent_runner 未调用 install_logger_bridge"
    assert "logger=logger_instance" in src, "logger 实例未注入 CoreDeps"

    src = (R3XT1_PKG_ROOT / "agent" / "loop.py").read_text(encoding="utf-8")
    assert "bind_log_context(" in src, "主循环未绑定 run 级日志上下文"
    assert "set_log_context(turn_index=ctx.turn_index)" in src, "主循环未逐轮刷新 turn_index"


@pytest.mark.asyncio
async def test_run_loop_wrapper_binds_run_scope_and_restores() -> None:
    """破坏者契约：主循环薄壳必须绑定 run 级上下文，且退出后**精确复原**。

    同时验证薄壳没有绕过绑定——用替身接住 `_run_loop_body`，观察其内部看到的上下文。
    """
    from bandi.agent.base import ExecutionContext
    from bandi.agent.loop import AgentLoop
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.llm.mock import MockProvider
    from bandi.observability.context import bind_log_context, current_log_context

    loop = AgentLoop()
    ctx = ExecutionContext(
        messages=[],
        provider=MockProvider(),
        config=AgentConfig(),
        bus=EventBus(),
        meta={"session_id": "sess-42", "agent_id": "a-9", "role": "MEMBER", "run_id": "run-7"},
    )
    seen: dict[str, object] = {}

    async def _capture(c: object, max_turns: int, turns_taken: int) -> None:
        seen["ctx"] = current_log_context()

    loop._run_loop_body = _capture  # type: ignore[method-assign]

    with bind_log_context(request_id="outer-req"):
        await loop._run_loop(ctx, max_turns=1, turns_taken=0)
        outer_after = current_log_context()

    inner = seen["ctx"]
    assert inner.session_id == "sess-42", f"run 级 session_id 未绑定：{inner}"
    assert inner.agent_id == "a-9", f"run 级 agent_id 未绑定：{inner}"
    assert inner.role == "MEMBER", f"run 级 role 未绑定：{inner}"
    assert inner.run_id == "run-7", f"run 级 run_id 未绑定：{inner}"
    assert inner.request_id == "outer-req", "薄壳覆盖了外层已绑定的 request_id"
    assert outer_after.request_id == "outer-req", "薄壳退出时破坏了外层上下文"
    assert current_log_context().session_id == "", "薄壳退出后上下文泄漏到调用方"


# ==================== t2_parallel_dispatch ====================
# 功能：契约——多 Agent 真并行调度
#
# 背景：`_dispatch_todo_items` 原是 `for session in sessions: ... await run_work_item(...)`
# 的**串行**循环，`max_concurrent` 只是「每 tick 最多挑几个」的预算而非真并发闸门 →
# 3 个员工的 3 个就绪项被依次执行，团队看板永远看不到并行态。
#
# 改造后两阶段：
# 阶段一（串行）：选任务 + 认领（store CAS / 座位认领——并发安全的关键全在这里）
# 阶段二（并行）：Semaphore(max_concurrent) + gather(return_exceptions=True)，**每项独立超时**
# 阶段三（串行落账）：report.* 与座位释放在 gather 之后统一做，杜绝并发写竞态
#
# 本契约钉死四条：并发峰值受控、单点失败不传染、超时按项隔离、座位不泄漏。


class R4xt2StubStore:
    def __init__(self, items: dict[str, Any]) -> None:
        self._items = items

    def get(self, work_item_id: str) -> Any:
        return self._items.get(work_item_id)


class _StubItem:
    def __init__(self, item_id: str, role: str) -> None:
        from bandi.company.phase import Phase

        self.id = item_id
        self.assignee_role = role
        self.phase = Phase.READY
        self.metadata: dict[str, Any] = {}


class _StubRuntimeState:
    def __init__(self) -> None:
        self.claimed_work_item_ids: set[str] = set()


class _StubRuntime:
    """只实现 `_dispatch_todo_items` 触达的接口。"""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._queue = list(pairs)
        self.state = _StubRuntimeState()
        self.enqueued: list[str] = []
        self.released: list[str] = []

    def enqueue_runnable_work_items(self, items: list[Any]) -> None:
        self.enqueued = [i.id for i in items]

    def ensure_role_member_session(self, role_id: str) -> None:
        return None

    def sorted_member_sessions(self) -> list[Any]:
        sessions = []
        for _wid, role in self._queue:
            sessions.append(type("S", (), {"role_id": role, "status": "idle"})())
        return sessions

    def pop_next_work_item_id(self, role_id: str) -> str | None:
        for idx, (_wid, r) in enumerate(self._queue):
            if r == role_id:
                return self._queue.pop(idx)[0]
        return None

    def is_dispatchable(self, item: Any) -> bool:
        return True

    def claim_work_item(self, work_item_id: str, *, role_id: str) -> None:
        self.state.claimed_work_item_ids.add(work_item_id)

    def release_work_item(self, work_item_id: str, *, role_id: str) -> None:
        self.state.claimed_work_item_ids.discard(work_item_id)
        self.released.append(work_item_id)


class _StubOrg:
    def list_agents(self) -> list[Any]:
        return []


def _build(
    *,
    n_items: int = 5,
    concurrency: int = 2,
    duration: float = 0.15,
    fail_on: set[str] | None = None,
    slow_on: set[str] | None = None,
    per_item_timeout: float = 10.0,
    failed_results: set[str] | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    """装配一个只含并行调度所需协作者的 dispatcher。"""
    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig

    dispatcher = object.__new__(CompanyDispatcher)
    dispatcher.config = DispatcherConfig(
        max_concurrent=concurrency, per_item_timeout_sec=per_item_timeout
    )
    dispatcher.org_engine = _StubOrg()
    dispatcher.seat_executor = object()  # 非 None → 走真执行路径

    items = {f"w{i}": _StubItem(f"w{i}", f"r{i}") for i in range(n_items)}
    dispatcher.store = R4xt2StubStore(items)
    runtime = _StubRuntime([(f"w{i}", f"r{i}") for i in range(n_items)])

    probe: dict[str, Any] = {
        "running": 0,
        "peak": 0,
        "order": [],
        "claimed_seats": [],
        "released_seats": [],
    }

    def _claim(role_id: str, _work_item_id: str = "") -> bool:
        probe["claimed_seats"].append(role_id)
        return True

    def _release(role_id: str, _work_item_id: str = "") -> bool:
        probe["released_seats"].append(role_id)
        return True

    dispatcher._active_claims = set()
    dispatcher.claim = _claim  # type: ignore[method-assign]
    dispatcher.release = _release  # type: ignore[method-assign]

    fails = fail_on or set()
    slows = slow_on or set()
    failed_res = failed_results or set()

    async def _run_work_item(work_item_id: str) -> Any:
        probe["running"] += 1
        probe["peak"] = max(probe["peak"], probe["running"])
        probe["order"].append(work_item_id)
        try:
            await asyncio.sleep(duration + (2.0 if work_item_id in slows else 0.0))
            if work_item_id in fails:
                raise RuntimeError(f"boom-{work_item_id}")
            if work_item_id in failed_res:
                return type(
                    "SeatResult", (), {"success": False, "output": f"failed-{work_item_id}"}
                )()
            return type("SeatResult", (), {"success": True, "output": ""})()
        finally:
            probe["running"] -= 1

    dispatcher.run_work_item = _run_work_item  # type: ignore[method-assign]
    return dispatcher, runtime, probe


def _report() -> Any:
    from bandi.company.company_mode import DispatcherReport

    return DispatcherReport(tick_id="t1", started_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_concurrency_cap_and_card_level_continuation() -> None:
    """契约：并发峰值受 max_concurrent 限制；单卡完成即补位（不再等整批 gather 结束）。

    旧语义「每 tick 最多挑 max_concurrent 张」会让慢卡拖住整批；卡级接续改为
    「有槽就补位」，因此一次派发可消费整个就绪队列（总量仍受 max_items_per_tick 硬顶）。
    """
    dispatcher, runtime, probe = _build(n_items=5, concurrency=2, duration=0.05)
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert probe["peak"] == 2, f"峰值并发 {probe['peak']} != 2（串行执行则为 1）"
    assert len(report.processed) == 5, (
        f"卡级接续：槽位释放后队列必须继续派发，实际 {report.processed}"
    )

    dispatcher, runtime, probe = _build(n_items=4, concurrency=2, duration=0.05, slow_on={"w0"})
    report = _report()
    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert set(report.processed) == {"w0", "w1", "w2", "w3"}, (
        f"慢卡不得饿死其余卡：{report.processed}"
    )
    assert probe["peak"] <= 2, f"峰值并发超过闸门：{probe['peak']}"
    assert report.processed[-1] == "w0", (
        f"慢卡应最后完成（其余卡在它等待期间已补位跑完），实际完成顺序 {report.processed}"
    )


@pytest.mark.asyncio
async def test_parallel_is_faster_than_serial() -> None:
    """契约：并发度提升必须体现在总耗时上（4 项 × 0.，并发 4 → 远低于串行 0.6s）。"""
    import time

    dispatcher, runtime, _probe = _build(n_items=4, concurrency=4, duration=0.15)
    report = _report()
    started = time.monotonic()
    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))
    elapsed = time.monotonic() - started

    assert elapsed < 0.4, f"耗时 {elapsed:.2f}s 接近串行上限 0.6s —— 未真正并行"


@pytest.mark.asyncio
async def test_single_failure_does_not_cascade() -> None:
    """破坏者契约：1 项抛异常不得传染——其余 4 项照常完成，errors 恰好 1 条。"""
    dispatcher, runtime, _probe = _build(n_items=5, concurrency=5, fail_on={"w2"})
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert len(report.errors) == 1, f"错误数应为 1，实际 {len(report.errors)}：{report.errors}"
    assert "w2" in report.errors[0]
    assert set(report.processed) == {"w0", "w1", "w3", "w4"}, (
        f"失败传染了：processed={report.processed}"
    )


@pytest.mark.asyncio
async def test_per_item_timeout_isolated_and_releases_claim_with_reason() -> None:
    """破坏者契约：单项超时只废掉该项（不整批取消），且释放 runtime 认领 + errors 携带根因。"""
    dispatcher, runtime, _probe = _build(
        n_items=4, concurrency=4, duration=0.05, slow_on={"w1"}, per_item_timeout=0.4
    )
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert set(report.processed) == {"w0", "w2", "w3"}, (
        f"超时项之外的项必须照常完成：processed={report.processed}"
    )
    assert len(report.errors) == 1 and "w1" in report.errors[0], (
        f"超时未记入 errors：{report.errors}"
    )
    assert "w1" not in runtime.state.claimed_work_item_ids, (
        "超时项未释放 runtime 认领（状态跨 tick 复用时将被永久跳过）"
    )
    assert "w1" in runtime.released, f"超时项必须走 release_work_item：{runtime.released}"
    assert "timeout" in report.errors[0].lower(), f"超时根因丢失：{report.errors}"


@pytest.mark.asyncio
async def test_seat_and_runtime_claims_released_on_success_and_failure() -> None:
    """契约：座位认领/释放一一对称；失败项同样释放座位；成功项从 runtime 认领集释放。"""
    dispatcher, runtime, probe = _build(n_items=5, concurrency=5)
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert len(probe["claimed_seats"]) == 5, f"应认领 5 个座位：{probe['claimed_seats']}"
    assert len(probe["claimed_seats"]) == len(probe["released_seats"]), (
        f"座位泄漏：认领 {probe['claimed_seats']} / 释放 {probe['released_seats']}"
    )
    assert len(report.claimed) == len(report.released), (
        f"报claimed/released 不对称：{report.claimed} / {report.released}"
    )

    dispatcher, runtime, probe = _build(n_items=4, concurrency=4, fail_on={"w1", "w3"})
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert len(probe["claimed_seats"]) == 4, f"应有 4 次认领：{probe['claimed_seats']}"
    assert len(probe["released_seats"]) == 4, f"失败项未释放座位：{probe['released_seats']}"

    dispatcher, runtime, _probe = _build(n_items=3, concurrency=3)
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert runtime.state.claimed_work_item_ids == set(), (
        f"runtime 认领集未清空：{runtime.state.claimed_work_item_ids}"
    )
    assert set(runtime.released) == {"w0", "w1", "w2"}


@pytest.mark.asyncio
async def test_report_timeline_and_transitions_recorded() -> None:
    """契约（DoR-6）：report.timeline 提供 per-agent 时间线；每项写 transitions。"""
    dispatcher, runtime, _probe = _build(n_items=3, concurrency=3, duration=0.05)
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert len(report.timeline) == 3, f"时间线条目缺失：{report.timeline}"
    for work_item_id, role_id, duration_ms, ok in report.timeline:
        assert work_item_id.startswith("w") and role_id.startswith("r")
        assert duration_ms >= 0.0
        assert ok is True
    assert {entry[0] for entry in report.timeline} == {"w0", "w1", "w2"}

    dispatcher, runtime, _probe = _build(n_items=4, concurrency=4)
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    recorded = {t[0] for t in report.transitions}
    assert recorded == {"w0", "w1", "w2", "w3"}, f"transitions 缺项：{recorded}"


@pytest.mark.asyncio
async def test_claim_happens_before_parallel_execution() -> None:
    """契约：认领必须发生在串行阶段——并行阶段开始时所有项已在 runtime 认领集内。

    （若把认领挪进并行分支，两个 tick/进程可能同时认领同一项 → 双执行。）
    """
    dispatcher, runtime, probe = _build(n_items=3, concurrency=3)
    seen_claimed_at_start: list[int] = []

    original = dispatcher.run_work_item

    async def _wrapped(work_item_id: str) -> Any:
        seen_claimed_at_start.append(len(runtime.state.claimed_work_item_ids))
        return await original(work_item_id)

    dispatcher.run_work_item = _wrapped  # type: ignore[method-assign]
    report = _report()
    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert seen_claimed_at_start and min(seen_claimed_at_start) == 3, (
        f"并行开始时认领集应有 3 项，实际 {seen_claimed_at_start}"
    )


@pytest.mark.asyncio
async def test_no_items_is_noop() -> None:
    """边界契约：没有就绪项时不报错、不误记。"""
    dispatcher, runtime, probe = _build(n_items=0, concurrency=2)
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, [])

    assert report.processed == []
    assert report.errors == []
    assert probe["peak"] == 0


@pytest.mark.asyncio
async def test_seat_result_failure_not_processed() -> None:
    """修复：`run_work_item` 返回 SeatResult(success=False)（不抛异常）也必须
    按失败处理——不计 processed、错误入 errors、timeline 标记 not ok。"""
    dispatcher, runtime, _probe = _build(n_items=4, concurrency=4, failed_results={"w1"})
    report = _report()

    await dispatcher._dispatch_todo_items(report, runtime, list(dispatcher.store._items.values()))

    assert set(report.processed) == {"w0", "w2", "w3"}, (
        f"失败结果被虚报为 processed：{report.processed}"
    )
    assert len(report.errors) == 1 and "w1" in report.errors[0], (
        f"失败结果未计入 errors：{report.errors}"
    )
    timeline_ok = {entry[0]: entry[3] for entry in report.timeline}
    assert timeline_ok["w1"] is False, f"timeline 未标记失败：{report.timeline}"
    assert timeline_ok["w0"] is True


# ==================== t3_denial_tracker ====================
# 功能：契约——连续拒绝追踪进生产链路（.2）
#
# 修复背景：「拒绝追踪」被差距清单标为已完成，实际唯一实现落在
# `company/approval.py`（工具面零生产调用的死链）里 → 生产从未生效（假完成）。
# 本契约钉死迁移后的语义与**生产接线**（`apply_decision` 记账 + `_approval_gate` 短路）。


def r5xt3_tracker(**kw: object) -> object:
    from bandi.safety.denial_tracker import DenialTracker

    return DenialTracker(**kw)  # type: ignore[arg-type]


def test_consecutive_threshold_approval_reset_and_stats() -> None:
    """契约：连续拒绝达阈值触发；批准清零连续保留累计；stats 口径正确。"""
    t = r5xt3_tracker()
    for i in range(2):
        t.record_denial("s1", "bash")  # type: ignore[attr-defined]
        assert not t.should_auto_deny("s1", "bash"), f"第 {i + 1} 次拒绝就触发了"  # type: ignore[attr-defined]
    t.record_denial("s1", "bash")  # type: ignore[attr-defined]
    assert t.should_auto_deny("s1", "bash")  # type: ignore[attr-defined]

    assert t.should_auto_deny("s1", "bash")  # type: ignore[attr-defined]
    t.record_approval("s1", "bash")  # type: ignore[attr-defined]
    stats = t.stats("s1", "bash")  # type: ignore[attr-defined]
    assert stats.consecutive == 0, f"批准后连续计数未清零：{stats}"
    assert stats.total == 3, f"累计计数不该被批准清零：{stats}"
    assert not t.should_auto_deny("s1", "bash")  # type: ignore[attr-defined]

    t = r5xt3_tracker()
    t.record_denial("s1", "bash")  # type: ignore[attr-defined]
    t.record_denial("s1", "bash")  # type: ignore[attr-defined]
    t.record_approval("s1", "bash")  # type: ignore[attr-defined]
    t.record_denial("s1", "bash")  # type: ignore[attr-defined]
    stats = t.stats("s1", "bash")  # type: ignore[attr-defined]
    assert (stats.consecutive, stats.total) == (1, 3), f"统计口径错误：{stats}"


def test_total_threshold_triggers_abort() -> None:
    """契约：累计拒绝达到阈值（默认 10）后应判定会话该中止。"""
    t = r5xt3_tracker()
    for _ in range(9):
        t.record_denial("s1", "bash")  # type: ignore[attr-defined]
        t.record_approval("s1", "bash")  # type: ignore[attr-defined]
    assert not t.should_abort("s1", "bash")  # type: ignore[attr-defined]
    t.record_denial("s1", "bash")  # type: ignore[attr-defined]
    assert t.should_abort("s1", "bash")  # type: ignore[attr-defined]


def test_expired_denials_are_ignored() -> None:
    """契约：超过 TTL 的拒绝记录失效——历史决策不应永久压制当前会话。"""
    t = r5xt3_tracker(ttl_seconds=0.05)
    for _ in range(3):
        t.record_denial("s1", "bash")  # type: ignore[attr-defined]
    assert t.should_auto_deny("s1", "bash")  # type: ignore[attr-defined]
    time.sleep(0.1)
    assert not t.should_auto_deny("s1", "bash"), "过期记录仍在生效"  # type: ignore[attr-defined]
    assert t.stats("s1", "bash").total == 0  # type: ignore[attr-defined]


def test_sessions_and_tools_are_isolated() -> None:
    """破坏者契约：一个会话/工具上的拒绝不得污染另一个。"""
    t = r5xt3_tracker()
    for _ in range(3):
        t.record_denial("s1", "bash")  # type: ignore[attr-defined]

    assert t.should_auto_deny("s1", "bash")  # type: ignore[attr-defined]
    assert not t.should_auto_deny("s2", "bash"), "跨会话串扰"  # type: ignore[attr-defined]
    assert not t.should_auto_deny("s1", "write"), "跨工具串扰"  # type: ignore[attr-defined]


def test_shared_tracker_is_singleton_and_resettable() -> None:
    """契约：进程级共享实例可重置（测试隔离依赖此点）。"""
    from bandi.safety.denial_tracker import get_denial_tracker, reset_denial_tracker

    reset_denial_tracker()
    a = get_denial_tracker()
    b = get_denial_tracker()
    assert a is b, "共享实例未复用"
    a.record_denial("s1", "bash")
    reset_denial_tracker()
    assert get_denial_tracker().stats("s1", "bash").total == 0, "重置未清空状态"


# ---------------------------------------------------------------- 生产接线

R5XT3_PKG_ROOT = Path(__file__).resolve().parents[2] / "bandi"


def test_wiring_apply_decision_records_denial_and_approval() -> None:
    """接线契约：人工 deny 记账；人工批准清零连续计数。"""
    import asyncio

    from bandi.safety.denial_tracker import get_denial_tracker
    from bandi.tools.approval import ApprovalStore, apply_decision

    store = ApprovalStore()
    tracker = get_denial_tracker()
    asyncio.run(apply_decision(store, "sess-w", "deny", "bash"))
    assert tracker.stats("sess-w", "bash").total == 1, "deny 未记账"

    store = ApprovalStore()
    for _ in range(3):
        asyncio.run(apply_decision(store, "sess-a", "deny", "bash"))
    assert tracker.should_auto_deny("sess-a", "bash")

    asyncio.run(apply_decision(store, "sess-a", "allow_session", "bash"))
    assert not tracker.should_auto_deny("sess-a", "bash"), "批准后连续计数未清零"


def test_wiring_short_circuit_and_test_isolation() -> None:
    """接线契约：`_approval_gate` 接入连续拒绝短路；conftest 重置追踪器防用例污染。"""
    src = (R5XT3_PKG_ROOT / "tools" / "executor.py").read_text(encoding="utf-8")
    assert "should_auto_deny" in src, "_approval_gate 未接入连续拒绝短路——能力仍是死的"
    assert "denial_tracker" in src or "get_denial_tracker" in src, "未引用拒绝追踪器"

    conftest = (Path(__file__).resolve().parents[1] / "conftest.py").read_text(encoding="utf-8")
    assert "reset_denial_tracker" in conftest, "conftest 未重置拒绝追踪器"


# ==================== t3_tool_risk_single_source ====================
# 功能：契约——工具风险分级单一来源（.1）
#
# * `company/approval.py`：`todo_write` ∈ CRITICAL_TOOLS
# * `tools/permissions_pipeline.py`：`todo_write` ∈ _BYPASS_TOOLS（免检放行）
# 改动任一处都不会被另一处感知——本契约把「风险分级」收敛到
# `bandi/safety/tool_risk.py` 单点，并钉死它与权限管道名单的关系。


R6XT3_PKG_ROOT = Path(__file__).resolve().parents[2] / "bandi"


def test_tool_risk_level_defined_once() -> None:
    """契约：**工具风险分级**（low/medium/high/critical）的定义全仓仅一处。

    ⚠️ 不能按类名判定：`bandi/tools/bash.py:55` 另有一个同名 `RiskLevel`，但它是
    bash AST 的安全判定结果（DENY/WARN/ALLOW），与工具风险分级是**不同概念**。
    因此按类体签名（`LOW = "low"` + `CRITICAL = "critical"`）识别本概念。
    """
    offenders: list[str] = []
    for py in sorted(R6XT3_PKG_ROOT.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        if py.name == "tool_risk.py" and py.parent.name == "safety":
            continue
        text = py.read_text(encoding="utf-8")
        if "class RiskLevel" not in text:
            continue
        if 'LOW = "low"' in text and 'CRITICAL = "critical"' in text:
            offenders.append(str(py.relative_to(R6XT3_PKG_ROOT.parent)).replace("\\", "/"))
    assert not offenders, f"工具风险分级 RiskLevel 被重复定义：{offenders}"


def test_bash_and_reorg_risk_levels_are_separate_concepts() -> None:
    """契约（边界澄清）：bash AST 判定与组织重构风险均刻意不与工具风险合并。"""
    from bandi.company.reorg_types import ReorgRiskLevel
    from bandi.safety import tool_risk as tool_risk_module
    from bandi.tools import bash as bash_module

    assert bash_module.RiskLevel is not tool_risk_module.RiskLevel, (
        "bash AST 判定与工具风险分级被错误合并"
    )
    assert {m.name for m in bash_module.RiskLevel} == {"DENY", "WARN", "ALLOW"}
    assert ReorgRiskLevel is not tool_risk_module.RiskLevel, (
        "ReorgRiskLevel 与工具 RiskLevel 被错误合并"
    )


def test_company_approval_reexports_same_objects() -> None:
    """契约：`company.approval` 的 RiskLevel 与分级函数必须与单一来源是同一对象。"""
    from bandi.company import approval as company_approval_module
    from bandi.company.approval import classify_tool_risk as compat
    from bandi.company.approval import predict_risk_from_arguments as compat_predict
    from bandi.safety import tool_risk as tool_risk_module
    from bandi.safety.tool_risk import classify_tool_risk as canonical
    from bandi.safety.tool_risk import predict_risk_from_arguments as canonical_predict

    assert company_approval_module.RiskLevel is tool_risk_module.RiskLevel, (
        "company.approval 的 RiskLevel 未 re-export 单一来源"
    )
    assert compat is canonical
    assert compat_predict is canonical_predict


def test_risk_tiers_are_mutually_exclusive() -> None:
    """破坏者契约：四档名单两两不得相交（同一工具不能既是 LOW 又是 CRITICAL）。"""
    from bandi.safety.tool_risk import (
        CRITICAL_TOOLS,
        HIGH_RISK_TOOLS,
        READ_ONLY_TOOLS,
        WRITE_TOOLS,
    )

    tiers = {
        "read_only": READ_ONLY_TOOLS,
        "write": WRITE_TOOLS,
        "high": HIGH_RISK_TOOLS,
        "critical": CRITICAL_TOOLS,
    }
    names = list(tiers)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            overlap = tiers[a] & tiers[b]
            assert not overlap, f"风险档 {a} 与 {b} 相交：{sorted(overlap)}"


def test_bypass_tools_are_never_high_or_critical_and_todo_write_resolved() -> None:
    """破坏者契约：免检工具不得判为 HIGH/CRITICAL；`todo_write` 必须为 LOW 且不在 CRITICAL。"""
    from bandi.safety.tool_risk import (
        CRITICAL_TOOLS,
        HIGH_RISK_TOOLS,
        RiskLevel,
        classify_tool_risk,
    )
    from bandi.tools.permissions_pipeline import _BYPASS_TOOLS

    risky = HIGH_RISK_TOOLS | CRITICAL_TOOLS
    conflicted = sorted(t for t in _BYPASS_TOOLS if t in risky)
    assert not conflicted, f"免检工具却被判高风险：{conflicted}"

    assert "todo_write" in _BYPASS_TOOLS, "前提变化：todo_write 已不在免检名单，需重新评估本契约"
    assert classify_tool_risk("todo_write") == RiskLevel.LOW
    assert "todo_write" not in CRITICAL_TOOLS


def test_classification_regression() -> None:
    """回归契约：迁移不得改变既有分级（唯一有意变更 = todo_write，见上一条）。"""
    from bandi.safety.tool_risk import RiskLevel, classify_tool_risk

    assert classify_tool_risk("read") == RiskLevel.LOW
    assert classify_tool_risk("list_dir") == RiskLevel.LOW
    assert classify_tool_risk("bash") == RiskLevel.MEDIUM
    assert classify_tool_risk("file_edit") == RiskLevel.MEDIUM
    assert classify_tool_risk("git_commit") == RiskLevel.HIGH
    assert classify_tool_risk("git_push") == RiskLevel.HIGH
    assert classify_tool_risk("browser_snapshot") == RiskLevel.HIGH
    assert classify_tool_risk("apply_patch") == RiskLevel.CRITICAL
    assert classify_tool_risk("shell_exec") == RiskLevel.CRITICAL
    assert classify_tool_risk("unknown_tool") == RiskLevel.MEDIUM


def test_predict_only_upgrades_never_downgrades() -> None:
    """契约：参数推断只会**上调**风险，绝不降级。"""
    from bandi.safety.tool_risk import RiskLevel, predict_risk_from_arguments

    assert predict_risk_from_arguments("bash", {"force": True}) == RiskLevel.CRITICAL
    assert predict_risk_from_arguments("git_commit", {"force": True}) == RiskLevel.CRITICAL
    # 高基准 + 低关键词 → 保持基准
    assert predict_risk_from_arguments("apply_patch", {"read": True}) == RiskLevel.CRITICAL
    # 关键词不高于基准 → 保持基准
    assert predict_risk_from_arguments("git_commit", {"delete": True}) == RiskLevel.HIGH
    # 无参数 → 基准
    assert predict_risk_from_arguments("bash", None) == RiskLevel.MEDIUM


# 第二套工具审批实现（对工具面零生产调用）已删除。以下用例防其被重新引入——
# 若将来有人又在本仓加一个「工具审批引擎」，这里会红并指向唯一生产链路。


@pytest.mark.parametrize(
    "removed",
    ["authorize_tool_call", "predict", "authorize", "add_to_allowlist", "is_allowlisted"],
)
def test_dead_tool_approval_api_is_gone(removed: str) -> None:
    """契约：已删除的第二套工具审批 API 不得复活。"""
    from bandi.company.approval import ApprovalEngine

    assert not hasattr(ApprovalEngine, removed), (
        f"ApprovalEngine.{removed} 复活了——工具审批的唯一生产链路是 "
        "tools/approval.py 的 ApprovalStore + executor._approval_gate"
    )


def test_approval_engine_public_surface_and_single_entry_point() -> None:
    """契约：`ApprovalEngine` 只保留工作项级授权；工具审批唯一入口只依赖 ApprovalStore。"""
    from bandi.company.approval import ApprovalEngine

    public = {n for n in dir(ApprovalEngine) if not n.startswith("_")}
    assert public == {"authorize_work_item_action"}, (
        f"ApprovalEngine 公开面意外变化：{sorted(public)}"
    )

    src = (R6XT3_PKG_ROOT / "tools" / "executor.py").read_text(encoding="utf-8")
    assert "get_approval_store" in src, "工具审批未走 ApprovalStore"
    assert "company.approval" not in src, "executor 又引入了第二套审批实现"


# ==================== t4_role_mapping ====================
#
# * `core/permission/roles.py` 的内置权限档（admin/power_user/member/readonly）
# 经 `PolicyEngine` 在工具执行面生效；
#


def _decision_for(role: str, role_map: dict[str, str] | None, action: str = "execute"):
    """按 executor 的用法构造一次角色策略裁决。"""
    from bandi.agent.factory import build_policy_engine
    from bandi.core.permission.models import PermissionContext, Resource

    engine = build_policy_engine(None, role, role_map)  # type: ignore[arg-type]
    resource = Resource(type="tool", name="bash", action=action, attributes={})
    return engine.check(resource, PermissionContext(user_id="u", role=role))


def test_resolve_role_passthrough_mapping_invalid_and_case_normalized() -> None:
    """"""
    from bandi.core.permission.role_mapping import resolve_role

    for role in ("engineer", "user", "member", "随便什么岗位"):
        assert resolve_role(role, None) == role
        assert resolve_role(role, {}) == role

    assert resolve_role("engineer", {"engineer": "member"}) == "member"
    assert resolve_role("boss", {"boss": "admin"}) == "admin"

    assert resolve_role("reviewer", {"engineer": "member"}) == "reviewer"

    assert resolve_role("engineer", {"engineer": "superuser"}) == "engineer"
    assert resolve_role("engineer", {"engineer": ""}) == "engineer"

    assert resolve_role("eng", {"eng": "MEMBER"}) == "member"
    assert resolve_role("eng", {"eng": " Admin "}) == "admin"


def test_validate_and_builtin_names_match_roles_module() -> None:
    """契约：自检能报出非法项；合法目标集必须与 `roles._BUILTIN` 的键一致。"""
    from bandi.core.permission.role_mapping import BUILTIN_ROLE_NAMES, validate_role_map
    from bandi.core.permission.roles import _BUILTIN

    assert validate_role_map({}) == []
    assert validate_role_map({"eng": "member"}) == []
    problems = validate_role_map({"eng": "root", "": "member"})
    assert len(problems) == 2, f"应报 2 个问题：{problems}"

    assert BUILTIN_ROLE_NAMES == frozenset(_BUILTIN), (
        f"合法目标集与内置角色漂移：{sorted(BUILTIN_ROLE_NAMES)} vs {sorted(_BUILTIN)}"
    )


# ---------------------------------------------------------------- 端到端生效


def test_mapped_role_enforces_execute_and_readonly_denies_write() -> None:
    """"""
    from bandi.core.permission.models import Decision

    decision = _decision_for("engineer", {"engineer": "member"})
    assert decision.decision == Decision.DENY, (
        f"映射到 member 后 execute 应被拒，实际 {decision.decision}（{decision.reason}）"
    )

    decision = _decision_for("auditor", {"auditor": "readonly"}, action="write")
    assert decision.decision == Decision.DENY


def test_default_empty_map_and_unmapped_role_do_not_change_behaviour() -> None:
    """"""
    from bandi.core.permission.models import Decision

    before_style = _decision_for("user", None)
    empty_map = _decision_for("user", {})
    assert before_style.decision == empty_map.decision, (
        f"空映射改变了行为：{before_style.decision} vs {empty_map.decision}"
    )
    assert empty_map.decision != Decision.DENY, (
        "默认（无映射）不得把 execute 判成 DENY——那会打断现有跑 bash 的所有 Agent"
    )

    decision = _decision_for("reviewer", {"engineer": "member"})
    assert decision.decision != Decision.DENY


# ---------------------------------------------------------------- 生产接线


def test_wiring_factory_passes_role_map(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """接线契约（行为断言）：装配点把配置 role_map 真实生效——engineer→member 后 bash 被拒。"""
    from bandi.agent import factory as factory_mod
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.permission.models import Decision, PermissionContext, Resource
    from bandi.tools.pipeline import ToolPipeline

    class ProviderStub:
        model_name = "stub"
        protocol = "openai"
        version = "1"

        def count_tokens(self, text: str) -> int:
            return 1

    class RegistryStub:
        def list_tools(self) -> list:
            return []

        def get(self, name: str):
            return None

        def list_schemas(self, allowed_tools=None) -> list:
            return []

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(factory_mod, "build_provider", lambda cfg: ProviderStub())
    monkeypatch.setattr(factory_mod, "build_tool_registry", lambda *a, **k: RegistryStub())
    monkeypatch.setattr(factory_mod, "build_tool_pipeline", lambda **k: ToolPipeline())

    cfg = BandiConfig()
    cfg.permissions.role_map = {"engineer": "member"}
    ctx = build_agent_context(
        user_input="wiring", agent_role="engineer", bandi_config=cfg, bus=EventBus()
    )
    engine = ctx.meta["policy_engine"]
    decision = engine.check(
        Resource(type="tool", name="bash", action="execute", attributes={}),
        PermissionContext(user_id="u", role="engineer"),
    )
    assert decision.decision == Decision.DENY, "配置 role_map 必须在装配点真实生效"


def test_config_defaults_to_empty_map_and_loads_from_dict() -> None:
    """契约：`PermissionsConfig.role_map` 默认为空，且可从配置字典加载。"""
    from bandi.core.config import PermissionsConfig

    assert PermissionsConfig().role_map == {}

    cfg = PermissionsConfig(role_map={"engineer": "member"})
    assert cfg.role_map == {"engineer": "member"}


@pytest.mark.parametrize("action", ["execute", "write", "delete"])
def test_readonly_denies_all_mutating_actions(action: str) -> None:
    """契约：readonly 对三类变更动作全部拒绝。"""
    from bandi.core.permission.models import Decision

    assert _decision_for("audit", {"audit": "readonly"}, action=action).decision == Decision.DENY


# ==================== t4_user_role_chain ====================
# 功能：契约——「用户 → 权限档」执行面直连（方案 A 续）。
#
# 背景：方案 A 已打通「岗位 → 权限档」（role_map，见 test_contract_t4_role_mapping.py）
# 但「用户 → 岗位」未投影进执行面——执行面只有 `_agent_id`，真实 `user_id` 到不了。
# 本契约钉死全链（前者优先）：
# 调用方 user_id → AccountStore.list_user_roles → 经 role_map 解析 → PolicyEngine 生效
# 并钉死零破坏：不传 user_id 时逐字节沿用旧行为（user_id 回落 _agent_id）。


def _memory_store(roles: list[str], user_id: str = "alice"):
    from bandi.core.account import AccountStore

    store = AccountStore(":memory:")
    store.create_user(user_id, "Alice", roles=roles)
    return store


class _ListRolesSpy:
    """记录 `list_user_roles` 调用痕迹的最小账户替身（零副作用契约用）。"""

    def __init__(self, roles: list[str] | None = None, exc: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._roles = roles or []
        self._exc = exc

    def list_user_roles(self, user_id: str) -> list[str]:
        self.calls.append(user_id)
        if self._exc is not None:
            raise self._exc
        return list(self._roles)


def _assembled_ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: Any) -> Any:
    """以生产装配形态构建 ctx（stub provider/registry，隔离 home）。"""
    from bandi.agent import factory as factory_mod
    from bandi.agent.factory import build_agent_context
    from bandi.core.config import BandiConfig
    from bandi.core.event_bus import EventBus
    from bandi.tools.pipeline import ToolPipeline

    class ProviderStub:
        model_name = "stub"
        protocol = "openai"
        version = "1"

        def count_tokens(self, text: str) -> int:
            return 1

    class RegistryStub:
        def list_tools(self) -> list:
            return []

        def get(self, name: str) -> Any | None:
            return None

        def list_schemas(self, allowed_tools: Any = None) -> list:
            return []

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(factory_mod, "build_provider", lambda cfg: ProviderStub())
    monkeypatch.setattr(factory_mod, "build_tool_registry", lambda *a, **k: RegistryStub())
    # 生产形态（ToolPipeline），否则假红
    monkeypatch.setattr(factory_mod, "build_tool_pipeline", lambda **k: ToolPipeline())
    kwargs.setdefault("bandi_config", BandiConfig())
    kwargs.setdefault("bus", EventBus())
    return build_agent_context(user_input="user-role 链契约", **kwargs)


# ---------------------------------------------------------------- 解析语义


def test_effective_role_resolution_semantics() -> None:
    """契约：绑定直接生效、经 role_map 解析、多岗位取最高、绑定优先、非档位回落。"""
    from bandi.core.permission.role_mapping import effective_role_for

    store = _memory_store(["member"])
    assert effective_role_for("user", user_id="alice", account_store=store) == "member"

    spy = _ListRolesSpy(["engineer"])
    got = effective_role_for(
        "user", user_id="e", role_map={"engineer": "power_user"}, account_store=spy
    )
    assert got == "power_user"

    store = _memory_store(["readonly", "admin"])
    assert effective_role_for("user", user_id="alice", account_store=store) == "admin"

    store = _memory_store(["member"])
    assert effective_role_for("power_user", user_id="alice", account_store=store) == "member"

    spy = _ListRolesSpy(["engineer"])
    assert effective_role_for("user", user_id="e", account_store=spy) == "user"


def test_empty_user_id_and_store_failure_do_not_block() -> None:
    """破坏者契约：无 user_id 不触碰账户库；绑定读取异常回落 agent_role 不抛。"""
    from bandi.core.permission.role_mapping import effective_role_for

    spy = _ListRolesSpy(["member"])
    assert effective_role_for("user", user_id="", account_store=spy) == "user"
    assert spy.calls == [], "user_id 为空时不得查询账户库"

    spy = _ListRolesSpy(exc=RuntimeError("db locked"))
    assert effective_role_for("engineer", user_id="e", account_store=spy) == "engineer"


# ---------------------------------------------------------------- 端到端生效


def test_bound_tier_enforces_and_no_user_id_keeps_zero_enforcement() -> None:
    """契约（全链核心价值）：绑定 member ⇒ bash execute 真 DENY；不传身份零策略注册。"""
    from bandi.agent.factory import build_policy_engine
    from bandi.core.permission.models import Decision, PermissionContext, Resource

    store = _memory_store(["member"])
    engine = build_policy_engine(None, "user", None, user_id="alice", account_store=store)
    decision = engine.check(
        Resource(type="tool", name="bash", action="execute", attributes={}),
        PermissionContext(user_id="alice", role="user"),
    )
    assert decision.decision == Decision.DENY, (
        f"绑定 member 后 execute 应被拒，实际 {decision.decision}（{decision.reason}）"
    )

    engine = build_policy_engine(None, "user", None)
    decision = engine.check(
        Resource(type="tool", name="bash", action="execute", attributes={}),
        PermissionContext(user_id="u", role="user"),
    )
    assert decision.decision != Decision.DENY


# ---------------------------------------------------------------- 接线（全链透传）


def test_build_agent_context_forwards_user_id_and_executor_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """接线契约：装配点把 user_id 带进 ctx.meta 与审批维度；未接入链路沿用 agent_id。"""
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    store = _memory_store(["member"])
    ctx = _assembled_ctx(monkeypatch, tmp_path, user_id="alice", account_store=store)

    assert ctx.meta.get("user_id") == "alice"
    assert ctx.tool_executor._approval_dims()["user_id"] == "alice"

    ex = ToolExecutor(ToolRegistry(), agent_id="worker-1")
    assert ex._approval_dims()["user_id"] == "worker-1"


def test_executor_propagates_user_id_to_tool_context() -> None:
    """执行面消费契约：user_id 真进了工具上下文（不只存构造属性）。"""
    import asyncio

    from bandi.core.event_bus import EventBus
    from bandi.core.message import ToolCall
    from bandi.tools.base import BaseTool, ToolCategory
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    seen: dict[str, Any] = {}

    class _EchoCtxTool(BaseTool):
        name = "echo_ctx"
        description = "stub"
        category = ToolCategory.UTIL

        async def execute(self, params: dict, context: Any = None) -> dict:
            seen["user_id"] = getattr(context, "user_id", None)
            return {"success": True, "output": "ok"}

    registry = ToolRegistry()
    registry.register_instance(_EchoCtxTool())
    ex = ToolExecutor(registry, agent_id="worker-1", user_id="alice", event_bus=EventBus())
    results = asyncio.run(ex.execute([ToolCall(id="t1", name="echo_ctx", arguments={})]))

    assert results and results[0].success, f"工具应执行成功：{results}"
    assert seen["user_id"] == "alice", "工具上下文必须拿到真实 user_id 而非 agent_id"


def test_api_chat_forwards_user_id_and_defaults_to_no_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """接线契约（生产入口）：/api/agent/chat 透传 user_id；未带时不得启用账户解析。"""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from bandi.api.app import app
    from bandi.core.agent_runner import AgentTurnResult

    captured: dict[str, Any] = {}

    async def _fake_turn(**kwargs: Any) -> AgentTurnResult:
        captured.update(kwargs)
        return AgentTurnResult(
            reply="ok",
            terminal_reason="stop",
            total_tokens=0,
            turn_index=0,
            session_id=str(kwargs.get("session_id") or "s"),
        )

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._account_store", None),
        patch("bandi.api.app._store", None),
        patch("bandi.api.app._db_path", tmp_path / "bandi.db"),
        patch("bandi.core.agent_runner.run_agent_turn", _fake_turn),
    ):
        client = TestClient(app)
        client.headers["Authorization"] = "Bearer test"
        resp = client.post("/api/agent/chat", json={"message": "hi", "user_id": "alice"})

        assert resp.status_code == 200, resp.text
        assert captured.get("user_id") == "alice", "user_id 必须透传到 run_agent_turn"
        assert captured.get("account_store") is not None, "user_id 非空时必须附带账户库"

        captured.clear()
        resp = client.post("/api/agent/chat", json={"message": "hi"})

    assert resp.status_code == 200, resp.text
    assert captured.get("user_id") == "", "未带 user_id 时必须保持空（不启用解析）"
    assert captured.get("account_store") is None, "未带 user_id 时不得触碰账户库"


# ---------------------------------------------------------------- 派生链路（子代理 / hooks）


def test_subagent_and_hook_agent_action_inherit_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接线契约：子代理装配与 hooks 的 agent 动作均继承用户身份（身份不因派生断链）。"""
    import asyncio

    from bandi.agent import subagent as sub_mod
    from bandi.agent import subagent_tool as st_mod
    from bandi.agent.subagent import SubagentResult
    from bandi.tools.base import ToolPermissionContext
    from bandi.tools.hooks import HookRule, HookRunner

    captured: dict[str, Any] = {}

    class _SpawnManyManager:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def spawn_many(self, *a: Any, **k: Any) -> list:
            return [SubagentResult(success=True, summary="ok")]

        def close(self) -> None:
            pass

    monkeypatch.setattr(st_mod, "SubagentManager", _SpawnManyManager)
    tool = st_mod.SubAgentTool()
    asyncio.run(tool.execute({"task": "做点事"}, ToolPermissionContext(user_id="alice")))
    assert captured.get("user_id") == "alice", "子代理装配必须继承父 user_id"

    captured.clear()

    class _SpawnManager:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def spawn(self, *a: Any, **k: Any) -> SubagentResult:
            return SubagentResult(success=False, summary="stub")

        def close(self) -> None:
            pass

    monkeypatch.setattr(sub_mod, "SubagentManager", _SpawnManager)
    runner = HookRunner(provider=object(), user_id="alice")
    rule = HookRule(event="PreToolUse", action_type="agent", prompt="检查")
    asyncio.run(runner._run_llm(rule, {}, as_agent=True))
    assert captured.get("user_id") == "alice", "hook agent 动作的子代理装配必须继承 user_id"


@pytest.mark.asyncio
async def test_run_agent_turn_forwards_identity_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """端到端契约：run_agent_turn 真实执行时把 user_id/account_store 送达装配点
    且装配出的策略引擎对绑定档真实生效（最终产物断言，非接口存在性）。"""
    from unittest.mock import patch

    from bandi.agent.factory import build_agent_context as _real_build
    from bandi.core.agent_runner import run_agent_turn
    from bandi.core.config import BandiConfig
    from bandi.core.permission.models import Decision, PermissionContext, Resource
    from bandi.memory.session.store import SessionStore

    captured: dict[str, Any] = {}
    captured_ctx: list[Any] = []

    def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        ctx = _real_build(**kwargs)
        captured_ctx.append(ctx)
        return ctx

    store = _memory_store(["member"])
    session_store = SessionStore(str(tmp_path / "sess.db"))
    sid = session_store.create_session(mode="execute")
    cfg = BandiConfig()
    cfg.agent.max_iterations = 1

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with patch("bandi.core.agent_runner.build_agent_context", side_effect=_capture):
        result = await run_agent_turn(
            user_input="身份链路验证",
            session_id=sid,
            session_store=session_store,
            bandi_config=cfg,
            user_id="alice",
            account_store=store,
        )
    session_store.close()

    assert result.reply is not None
    assert captured.get("user_id") == "alice", "run_agent_turn 必须把 user_id 送达装配点"
    assert captured.get("account_store") is store, "账户库必须随 user_id 一并送达"
    engine = captured_ctx[0].meta["policy_engine"]
    decision = engine.check(
        Resource(type="tool", name="bash", action="execute", attributes={}),
        PermissionContext(user_id="alice", role="user"),
    )
    assert decision.decision == Decision.DENY, "绑定 member 的档位必须经真实装配生效"


# ---------------------------------------------------------------- 绑定读取失败策略（fail-closed）
#
# 背景：`effective_role_for` 在岗位绑定读取失败时静默回落 `agent_role`——若
# 契约：
# 1. 失败必发 warning，且包含 user_id 与异常内容（可观测）；
# 2. 默认（strict=False / 未配置）行为与现状一致：告警后回落 agent_role；
# 3. 严格模式（strict=True 或 config `permissions.strict_role_binding: true`）：
# 抛 `BandiError` fail-closed——宁可拒绝执行，不得静默回落。


def _broken_store() -> _ListRolesSpy:
    return _ListRolesSpy(exc=RuntimeError("db locked"))


def test_binding_failure_warning_includes_user_and_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """契约（可观测）：读取失败必须告警，且携带 user_id 与异常信息。"""
    import logging

    from bandi.core.permission import role_mapping

    with caplog.at_level(logging.WARNING, logger="bandi.core.permission.role_mapping"):
        got = role_mapping.effective_role_for(
            "engineer", user_id="alice", account_store=_broken_store(), strict=False
        )
    assert got == "engineer", "非严格模式必须回落 agent_role"
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("alice" in m and "db locked" in m for m in messages), (
        f"告警必须含 user_id 与异常：{messages}"
    )


def test_strict_binding_fallback_and_raise_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """契约：默认回落 agent_role；配置严格模式抛 BandiError；显式 strict 覆盖配置。"""
    from bandi.core.exceptions import BandiError
    from bandi.core.permission import role_mapping

    monkeypatch.setattr(role_mapping, "strict_role_binding_enabled", lambda: False)
    got = role_mapping.effective_role_for("member", user_id="alice", account_store=_broken_store())
    assert got == "member", "默认必须回落 agent_role（现有链路零破坏）"

    monkeypatch.setattr(role_mapping, "strict_role_binding_enabled", lambda: True)
    with pytest.raises(BandiError, match="岗位绑定"):
        role_mapping.effective_role_for("admin", user_id="alice", account_store=_broken_store())

    with pytest.raises(BandiError):
        role_mapping.effective_role_for(
            "admin", user_id="alice", account_store=_broken_store(), strict=True
        )
    got = role_mapping.effective_role_for(
        "engineer", user_id="alice", account_store=_broken_store(), strict=False
    )
    assert got == "engineer"


def test_config_defaults_strict_binding_off() -> None:
    """契约（零破坏）：`permissions.strict_role_binding` 默认必须为 False。"""
    from bandi.core.config import PermissionsConfig

    assert PermissionsConfig().strict_role_binding is False


def test_strict_binding_loads_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """接线契约：`permissions.strict_role_binding: true` 经真实配置加载可达
    （不能只是数据类字段而永远读不到）。"""
    import os

    from bandi.core.permission import role_mapping

    home = tmp_path / "home"
    (home / ".bandi").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("BANDI_"):
            monkeypatch.delenv(key, raising=False)
    (home / ".bandi" / "config.yaml").write_text(
        "permissions:\n  strict_role_binding: true\n", encoding="utf-8"
    )
    role_mapping.strict_role_binding_enabled.cache_clear()
    try:
        assert role_mapping.strict_role_binding_enabled() is True
    finally:
        # lru_cache 是进程级：本测试必须还原，防污染后续默认回落用例
        role_mapping.strict_role_binding_enabled.cache_clear()


# ==================== t5_db_unified ====================
# 功能：契约——SQLite 连接统一入口（DB 健壮性统一）
#
# 背景：21 处连接点各写各的参数（timeout/check_same_thread/row_factory/WAL 有无不一）
# 无统一口径。本契约钉死三件事：
# 1. 统一入口 `bandi.utils.db.connect` 的行为默认值 = stdlib，可零风险替换；
# 2. WAL 只对应用自有库开启——外部数据源绝不擅改（破坏者用例）；
# 3. 全仓除统一入口外不得再出现裸 `sqlite3.connect(`（覆盖度硬约束）。


R9XT5_PKG_ROOT = Path(__file__).resolve().parents[2] / "bandi"


def test_connect_defaults_match_stdlib() -> None:
    """契约：统一入口的默认行为与 stdlib 一致——调用方仅换函数名，行为零变化。"""
    from bandi.utils.db import connect

    conn = connect(":memory:")
    try:
        raw = sqlite3.connect(":memory:")
        try:
            assert conn.isolation_level == raw.isolation_level
            assert conn.row_factory is None
            for pragma in ("busy_timeout", "foreign_keys", "query_only"):
                assert (
                    conn.execute(f"PRAGMA {pragma}").fetchone()[0]
                    == raw.execute(f"PRAGMA {pragma}").fetchone()[0]
                ), f"{pragma} 默认值必须与 stdlib 一致"
        finally:
            raw.close()
    finally:
        conn.close()


def test_timeout_maps_to_busy_timeout_and_explicit_override() -> None:
    """契约：timeout 参数即 busy_timeout（秒→毫秒）；显式 busy_timeout_ms 覆盖推导值。"""
    from bandi.utils.db import connect

    for seconds, expected_ms in ((5.0, 5000), (10.0, 10000), (30.0, 30000)):
        conn = connect(":memory:", timeout=seconds)
        try:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == expected_ms
        finally:
            conn.close()

    conn = connect(":memory:", timeout=5.0, busy_timeout_ms=1234)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    finally:
        conn.close()


def test_connect_app_db_enables_wal(tmp_path: Path) -> None:
    """契约：应用自有库走 connect_app_db 后 journal_mode 为 wal。"""
    from bandi.utils.db import connect_app_db

    conn = connect_app_db(tmp_path / "app.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_connect_app_db_is_cross_thread_safe(tmp_path: Path) -> None:
    """契约：应用自有库连接必须可跨线程复用（check_same_thread=False）。

    反证：python 3.12 起 sqlite3.connect 默认 check_same_thread=True——多线程
    存储（API 与后台线程共库）会在运行时抛 ProgrammingError。
    """
    from bandi.utils.db import connect_app_db

    conn = connect_app_db(tmp_path / "threadsafe.db")
    errors: list[Exception] = []

    def _use() -> None:
        try:
            conn.execute("SELECT 1").fetchone()
        except Exception as exc:  # noqa: BLE001 — 捕获后由断言判定
            errors.append(exc)

    try:
        t = threading.Thread(target=_use)
        t.start()
        t.join()
        assert not errors, f"跨线程使用失败：{errors}"
    finally:
        conn.close()


def test_wal_not_enabled_for_default_or_external_datasource(tmp_path: Path) -> None:
    """破坏者契约：默认 wal=False；外部 sqlite 数据源适配器不得把用户库改成 WAL。"""
    from bandi.datasource.adapters.sqlite import SqliteAdapter
    from bandi.utils.db import connect

    db = tmp_path / "external.db"
    conn = connect(db)
    conn.execute("CREATE TABLE t (x INT)")
    conn.commit()
    conn.close()

    conn = connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
    finally:
        conn.close()

    db = tmp_path / "user_data.db"
    seed = sqlite3.connect(str(db))
    seed.execute("CREATE TABLE t (x INT)")
    seed.execute("INSERT INTO t VALUES (1)")
    seed.commit()
    seed.close()

    adapter = SqliteAdapter({"path": str(db)})
    conn = adapter._connect()
    conn.close()

    check = sqlite3.connect(str(db))
    try:
        mode = check.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() != "wal", f"外部数据源库被擅自改为 WAL：{mode}"
    finally:
        check.close()


def test_concurrent_writes_do_not_raise_locked(tmp_path: Path) -> None:
    """契约：两个连接并发写同一应用库不抛 'database is locked'。"""
    from bandi.utils.db import connect_app_db

    db = tmp_path / "concurrent.db"
    setup = connect_app_db(db)
    setup.execute("CREATE TABLE t (x INT)")
    setup.commit()
    setup.close()

    errors: list[Exception] = []

    def _write(n: int) -> None:
        conn = connect_app_db(db)
        try:
            for i in range(20):
                conn.execute("INSERT INTO t VALUES (?)", (n * 100 + i,))
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写失败：{errors}"

    verify = connect_app_db(db)
    try:
        assert verify.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 40
    finally:
        verify.close()


def test_secure_db_file_platform_noop_and_missing_path(tmp_path: Path) -> None:
    """契约：Windows 上 secure_db_file 为 no-op；路径不存在时静默返回不抛。"""
    import os

    from bandi.utils.db import secure_db_file

    db = tmp_path / "perm.db"
    db.write_bytes(b"")

    if os.name == "nt":
        secure_db_file(db)  # 不得抛
    else:
        secure_db_file(db)
        assert (db.stat().st_mode & 0o777) == 0o600

    secure_db_file(tmp_path / "nope.db")  # 不得抛
    secure_db_file(":memory:")  # 不得抛


def test_no_direct_sqlite3_connect_outside_unified_entry() -> None:
    """覆盖度契约：全仓除 bandi/utils/db.py 外，不得再出现裸 `sqlite3.connect(`。

    这是 的完成度硬约束——新增连接点若绕过统一入口，本用例即红。
    """
    offenders: list[str] = []
    for py in sorted(R9XT5_PKG_ROOT.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        if py.name == "db.py" and py.parent.name == "utils":
            continue
        text = py.read_text(encoding="utf-8")
        if "sqlite3.connect(" in text:
            rel = py.relative_to(R9XT5_PKG_ROOT.parent)
            offenders.append(str(rel).replace("\\", "/"))

    assert not offenders, (
        "以下文件仍直接调用 sqlite3.connect(，必须改走 bandi.utils.db：\n  "
        + "\n  ".join(offenders)
    )


# ==================== t6_service_governance ====================
# 功能：契约——服务化治理（统一错误码 / 请求体上限 / 限流 / request_id / ready / 租户边界 / host 守卫）。
#
# 本契约钉死七项服务化能力的**行为结果**（响应体形状 / 状态码 / 头 / 隔离）
# 而非实现存在性。


def _api_module():
    """取 bandi.api.app 模块对象（注意：bandi.api.app 属性是 FastAPI 实例
    字符串 patch 会解析到实例——必须经 sys.modules 拿模块）。"""
    import bandi.api.app  # noqa: F401 — 确保已 import

    return sys.modules["bandi.api.app"]


def _client_with_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, api_key: str = "test"):
    """"""
    from fastapi.testclient import TestClient

    api_mod = _api_module()

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(api_mod, "_API_KEY", api_key)
    monkeypatch.setattr(api_mod, "_account_store", None)
    monkeypatch.setattr(api_mod, "_store", None)
    monkeypatch.setattr(api_mod, "_db_path", tmp_path / "bandi.db")
    client = TestClient(api_mod.app)
    client.headers["Authorization"] = f"Bearer {api_key}"
    return client


# ------------------------------------------------------------------ .1 统一错误响应体


def test_unified_error_body_across_failure_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约：400/404/401/422/405 错误响应体统一为 {code, message, detail, request_id}。"""
    client = _client_with_isolation(monkeypatch, tmp_path)

    # 400：缺 message
    r400 = client.post("/api/agent/chat", json={})
    # 404：会话不存在
    r404 = client.post("/api/agent/chat", json={"message": "hi", "session_id": "nope"})

    for resp, expect_status in ((r400, 400), (r404, 404)):
        assert resp.status_code == expect_status, resp.text
        body = resp.json()
        assert set(body.keys()) == {"code", "message", "detail", "request_id"}, (
            f"错误体形状不统一：{body}"
        )
        assert isinstance(body["code"], str) and body["code"]
        assert isinstance(body["request_id"], str)

    assert r400.json()["code"] == "VALIDATION_ERROR"
    assert r404.json()["code"] == "NOT_FOUND"

    resp = client.get("/api/sessions", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "detail", "request_id"}
    assert body["code"] == "UNAUTHORIZED"

    resp = client.post(
        "/api/agent/chat",
        content=b"{invalid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "detail", "request_id"}
    assert body["code"] == "VALIDATION_ERROR"

    resp = client.delete("/health")
    assert resp.status_code == 405
    assert resp.json()["code"] == "METHOD_NOT_ALLOWED"


# ------------------------------------------------------------------ .2 请求体上限


def test_request_body_limit_413(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """破坏者：超过配置上限的请求体必须 413 + 统一错误体（不进入端点处理）。"""
    client = _client_with_isolation(monkeypatch, tmp_path)
    monkeypatch.setattr(_api_module(), "_MAX_BODY_BYTES", 2048)

    payload = {"message": "x" * 5000}
    resp = client.post("/api/agent/chat", json=payload)
    assert resp.status_code == 413, f"超限请求体未拦截：{resp.status_code}"
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "detail", "request_id"}
    assert body["code"] == "PAYLOAD_TOO_LARGE"

    # 正常体量不受影响（防误伤）
    small = client.post("/api/agent/chat", json={"message": "hi", "session_id": "nope"})
    assert small.status_code == 404, "小请求体被体积中间件误伤"


# ------------------------------------------------------------------ .3 限流


def test_rate_limit_429_and_keyed_by_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """破坏者：同来源超限 → 429 + RATE_LIMITED + Retry-After；轮换 Bearer 前缀不得绕过限流。"""
    client = _client_with_isolation(monkeypatch, tmp_path)
    limiter = _api_module()._rate_limiter
    monkeypatch.setattr(limiter, "_max_calls", 3)
    monkeypatch.setattr(limiter, "_calls", {})

    statuses = [client.get("/api/sessions").status_code for _ in range(5)]
    assert 429 in statuses, f"未触发限流：{statuses}"
    first_429 = next(i for i, s in enumerate(statuses) if s == 429)
    assert first_429 == 3, f"窗口上限语义错（应第 4 次拒绝）：{statuses}"

    resp = client.get("/api/sessions")
    assert resp.status_code == 429
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "detail", "request_id"}
    assert body["code"] == "RATE_LIMITED"
    assert "Retry-After" in resp.headers

    monkeypatch.setattr(limiter, "_max_calls", 3)
    monkeypatch.setattr(limiter, "_calls", {})
    statuses = [
        client.get("/api/sessions", headers={"Authorization": f"Bearer tk{i:06d}"}).status_code
        for i in range(5)
    ]
    assert statuses[:3] == [401, 401, 401], f"前 3 次应认证失败：{statuses}"
    assert 429 in statuses[3:], f"轮换 token 绕过了限流：{statuses}"


# ------------------------------------------------------------------ .4 request_id + 安全头


def test_request_id_echo_generate_error_body_and_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约：透传/生成 X-Request-ID；错误体 request_id 与响应头一致；超长值被截断。"""
    client = _client_with_isolation(monkeypatch, tmp_path)

    given = client.get("/api/sessions", headers={"X-Request-ID": "req-abc-123"})
    assert given.headers.get("X-Request-ID") == "req-abc-123"

    generated = client.get("/api/sessions")
    rid = generated.headers.get("X-Request-ID")
    assert rid and rid != "req-abc-123"

    resp = client.post("/api/agent/chat", json={}, headers={"X-Request-ID": "trace-9"})
    assert resp.status_code == 400
    assert resp.json()["request_id"] == "trace-9"
    assert resp.headers.get("X-Request-ID") == "trace-9"

    resp = client.get("/api/sessions", headers={"X-Request-ID": "x" * 5000})
    assert resp.status_code == 200
    assert len(resp.headers.get("X-Request-ID", "")) <= 128


def test_security_headers_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """契约：标准安全响应头存在。"""
    client = _client_with_isolation(monkeypatch, tmp_path)
    resp = client.get("/health")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in resp.headers


# ------------------------------------------------------------------ .5 /ready 探针


def test_ready_probe_ok_and_503_while_health_stays_live(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约：依赖可用时 /ready 200；不可写时 503；/health 保持 liveness 无依赖 200。"""
    client = _client_with_isolation(monkeypatch, tmp_path)
    resp = client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ready"

    monkeypatch.setattr(_api_module(), "_probe_storage_writable", lambda: False)
    resp = client.get("/ready")
    assert resp.status_code == 503

    resp = client.get("/health")
    assert resp.status_code == 200


# ------------------------------------------------------------------ .6 多租户边界


def test_tenant_isolation_listing_and_store_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """破坏者：API 租户过滤生效且不带参数保持全量；AccountStore 过滤为真实查询谓词。"""
    from bandi.core.account import AccountStore

    client = _client_with_isolation(monkeypatch, tmp_path)

    client.post("/api/account/users", json={"user_id": "alice", "display_name": "A"})
    client.post(
        "/api/account/users",
        json={"user_id": "bob", "display_name": "B", "tenant_id": "acme"},
    )

    scoped = client.get("/api/account/users", params={"tenant_id": "acme"})
    assert scoped.status_code == 200
    ids = [u["user_id"] for u in scoped.json()["users"]]
    assert "bob" in ids and "alice" not in ids, f"租户边界失效：{ids}"

    unscoped = client.get("/api/account/users")
    all_ids = [u["user_id"] for u in unscoped.json()["users"]]
    assert {"alice", "bob"} <= set(all_ids), "不带 tenant_id 时应保持全量（零破坏）"

    store = AccountStore(tmp_path / "account.db")
    store.create_user("alice", "Alice")
    store.create_user("bob", "Bob", tenant_id="acme")

    assert [u.user_id for u in store.list_users(tenant_id="acme")] == ["bob"]
    assert {u.user_id for u in store.list_users()} == {"alice", "bob"}


# ------------------------------------------------------------------ .7 host 守卫 + 鉴权加固


def test_serve_host_guard_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏者：非 loopback 监听且未配置 BANDI_API_KEY 必须拒绝启动。"""
    from bandi.cli.app import validate_serve_host

    monkeypatch.delenv("BANDI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BANDI_API_KEY"):
        validate_serve_host("0.0.0.0")
    with pytest.raises(RuntimeError, match="BANDI_API_KEY"):
        validate_serve_host("192.168.1.10")

    # loopback 无需 key
    validate_serve_host("127.0.0.1")
    validate_serve_host("localhost")

    # 非 loopback + 有 key → 放行
    monkeypatch.setenv("BANDI_API_KEY", "secret")
    validate_serve_host("0.0.0.0")


def test_bearer_constant_time_http_and_ws(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """契约（鉴权加固）：HTTP/WS 共用 Bearer 校验走 hmac.compare_digest 且非 ASCII 不崩。"""
    import hmac

    client = _client_with_isolation(monkeypatch, tmp_path)

    calls: list[tuple] = []
    real = hmac.compare_digest

    def _spy(a: object, b: object) -> bool:
        calls.append((a, b))
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(hmac, "compare_digest", _spy)
    ok = client.get("/api/sessions")
    assert ok.status_code == 200
    assert calls, "Bearer 比较未使用 hmac.compare_digest"

    bad = client.get("/api/sessions", headers={"Authorization": "Bearer wrong-key"})
    assert bad.status_code == 401

    api_mod = _api_module()
    monkeypatch.setattr(api_mod, "_API_KEY", "secret")
    check = api_mod._bearer_token_ok
    assert check("secret") is True
    assert check("wrong") is False
    assert check("") is False
    assert check("\xe9") is False  # 非 ASCII：不抛异常，直接 False


@pytest.mark.asyncio
async def test_truncated_chunked_body_rejected_without_endpoint_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """破坏者：chunked 超限请求不得以 413 为掩护执行端点副作用。

    攻击形态：合法 JSON + 尾部填充（前段已构成完整 JSON）——截断流若让内层解析成功
    端点真实执行（写库/LLM/工具），客户端却只看到 413。
    用 ASGI 直调精确控制 receive 边界（TestClient 会聚合 body，测不出截断路径）。
    """
    import json as _json

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    api_mod = _api_module()
    monkeypatch.setattr(api_mod, "_API_KEY", "test")
    monkeypatch.setattr(api_mod, "_account_store", None)
    monkeypatch.setattr(api_mod, "_store", None)
    monkeypatch.setattr(api_mod, "_db_path", tmp_path / "bandi.db")
    monkeypatch.setattr(api_mod, "_MAX_BODY_BYTES", 2048)

    payload = _json.dumps({"user_id": "evil", "display_name": "attacker"}).encode("utf-8")
    parts: list[dict] = [
        {"type": "http.request", "body": payload, "more_body": True},
        {"type": "http.request", "body": b" " * 1024, "more_body": True},
        {"type": "http.request", "body": b" " * 1024, "more_body": True},
        {"type": "http.request", "body": b" " * 1024, "more_body": False},
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/account/users",
        "raw_path": b"/api/account/users",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer test"),
            (b"content-type", b"application/json"),
            (b"host", b"test"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return parts.pop(0) if parts else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await api_mod.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 413, f"chunked 超限未被拒：{start['status']}"

    store = api_mod._get_account_store()
    assert "evil" not in [u.user_id for u in store.list_users()], (
        "413 掩护下端点副作用已发生（用户被创建）"
    )


@pytest.mark.asyncio
async def test_non_ascii_bearer_returns_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """破坏者：非 ASCII Bearer 必须 401（此前 compare_digest(str) 抛 TypeError → 500）。

    httpx 客户端拒发非 ASCII header，用 ASGI 直调发送原始字节（真实 uvicorn/h11 形态）。
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    api_mod = _api_module()
    monkeypatch.setattr(api_mod, "_API_KEY", "test")
    monkeypatch.setattr(api_mod, "_account_store", None)
    monkeypatch.setattr(api_mod, "_store", None)
    monkeypatch.setattr(api_mod, "_db_path", tmp_path / "bandi.db")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/sessions",
        "raw_path": b"/api/sessions",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer \xe9"),  # 原始字节 0xE9（latin-1 解码为非 ASCII）
            (b"host", b"test"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await api_mod.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401, f"非 ASCII token 未走 401：{start['status']}"


def test_rate_limiter_prunes_idle_keys() -> None:
    """契约：限流器空闲 key 必须被回收（防未认证前缀轮换导致字典无界增长）。"""
    import asyncio
    import time as _time

    from bandi.tools.middleware.ratelimit_mw import SlidingWindowRateLimiter

    limiter = SlidingWindowRateLimiter(max_calls=10, window_sec=0.05, max_keys=64)
    for i in range(200):
        asyncio.run(limiter.check(f"k{i}"))
    _time.sleep(0.12)  # 全部过期
    asyncio.run(limiter.check("fresh"))
    assert len(limiter._calls) <= 65, f"空闲 key 未回收：{len(limiter._calls)}"


@pytest.mark.asyncio
async def test_ready_probe_concurrent_and_offloads_blocking_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """破坏者：并发探测不得假 503；/ready 的同步阻塞探针必须在线程池执行（不冻结循环）。"""
    import asyncio
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    from httpx import ASGITransport

    probe = _api_module()._probe_storage_writable
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda _: probe(), range(400)))
    assert all(results), f"并发探测出现假阴性 {results.count(False)}/400"

    api_mod = _api_module()
    monkeypatch.setattr(api_mod, "_API_KEY", "test")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _slow_probe() -> bool:
        _time.sleep(0.4)
        return True

    monkeypatch.setattr(api_mod, "_probe_db_readable", _slow_probe)

    transport = ASGITransport(app=api_mod.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        started = _time.monotonic()
        ready_task = asyncio.create_task(ac.get("/ready"))
        await asyncio.sleep(0.05)
        health_resp = await ac.get("/health")
        health_elapsed = _time.monotonic() - started
        await ready_task

    assert health_resp.status_code == 200
    assert health_elapsed < 0.3, f"/health 被同步探针阻塞（{health_elapsed:.2f}s）"


def test_500_unified_body_with_request_id_and_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """破坏者：未捕获异常必须 500 + 统一体 + request_id + 安全头。"""
    from fastapi.testclient import TestClient

    _client_with_isolation(monkeypatch, tmp_path)  # 建立隔离环境与 _API_KEY
    api_mod = _api_module()

    def _boom() -> None:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(api_mod, "_get_store", _boom)
    raw = TestClient(api_mod.app, raise_server_exceptions=False)
    raw.headers["Authorization"] = "Bearer test"
    resp = raw.get("/api/sessions", headers={"X-Request-ID": "crash-1"})

    assert resp.status_code == 500
    body = resp.json()
    assert set(body.keys()) == {"code", "message", "detail", "request_id"}
    assert body["code"] == "INTERNAL_ERROR"
    assert body["request_id"] == "crash-1"
    assert resp.headers.get("X-Request-ID") == "crash-1"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_cors_headers_on_middleware_generated_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约：中间件生成的 429/413 也必须带 CORS 头（跨域前端可读错误体）。"""
    client = _client_with_isolation(monkeypatch, tmp_path)
    limiter = _api_module()._rate_limiter
    monkeypatch.setattr(limiter, "_max_calls", 0)
    monkeypatch.setattr(limiter, "_calls", {})

    resp = client.get("/api/sessions", headers={"Origin": "http://localhost:3000"})
    assert resp.status_code == 429
    assert resp.headers.get("access-control-allow-origin"), "429 缺 CORS 头"

    monkeypatch.setattr(limiter, "_max_calls", 300)
    monkeypatch.setattr(_api_module(), "_MAX_BODY_BYTES", 256)
    resp2 = client.post(
        "/api/agent/chat",
        json={"message": "x" * 1000},
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp2.status_code == 413
    assert resp2.headers.get("access-control-allow-origin"), "413 缺 CORS 头"


def test_cors_preflight_not_blocked_by_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约：浏览器预检（OPTIONS，无凭证）必须放行（否则带 key 部署下跨域全断）。"""
    from fastapi.testclient import TestClient

    _client_with_isolation(monkeypatch, tmp_path)  # 建立隔离环境与 _API_KEY
    api_mod = _api_module()
    naked = TestClient(api_mod.app)  # 无 Authorization：真实预检形态
    resp = naked.options(
        "/api/sessions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert resp.status_code in (200, 204), f"预检被拦截：{resp.status_code}"
    assert resp.headers.get("access-control-allow-origin")
