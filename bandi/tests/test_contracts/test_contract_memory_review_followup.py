# 功能：契约——memory 域复查加固：SM 快照 TTL 豁免 / 工具历史 FTS 行一致 /
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

# ---------- SM 快照豁免 TTL ----------


def test_contract_sm_snapshot_survives_ttl(tmp_path: Path) -> None:
    """契约（会话记忆生命周期·破坏者）：turn: 快照（SM 摘要）不得被 working TTL
    清理——修复前与普通 KV 同寿（2h），>2h 会话边界快照消失 → 旧消息被误判
    「已覆盖」而永久丢弃。"""
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "sp.db"))
    store.set("turn:1", "会话摘要甲", session_id="s")
    store.set("k1", "普通中间值", session_id="s")

    store._ttl = -1  # 令所有行立即"过期"（惰性清理在 get 时触发）
    assert store.get("k1", "s") is None, "普通键应按 TTL 清理"
    assert store.get("turn:1", "s") == "会话摘要甲", (
        "SM 快照不得被 TTL 清理（会话记忆须与消息同生命周期，由 clear 清理）"
    )


# ---------- 工具历史 upsert 的 FTS 行一致 ----------


def test_contract_working_tool_upsert_fts_correct_row(tmp_path: Path) -> None:
    """契约（FTS 行一致·破坏者）：同 invocation_id 更新后 FTS 必须索引新内容——
    upsert 冲突路径 lastrowid 不可靠（保持跨 session 旧值）→ 索引错行：
    新内容不可召回、旧文本残留。"""
    from bandi.memory.working import ScratchpadStore, ToolInvocation

    store = ScratchpadStore(str(tmp_path / "sp.db"))
    store.append_tool(
        ToolInvocation(name="t", result_preview="甲甲甲唯一词", session_id="A", invocation_id="i1")
    )
    store.append_tool(
        ToolInvocation(name="t", result_preview="乙乙乙唯一词", session_id="B", invocation_id="i2")
    )
    store.append_tool(
        ToolInvocation(name="t", result_preview="丙丙丙新词", session_id="A", invocation_id="i1")
    )

    hits_new = store.search("丙丙丙新词", session_id="A")
    assert any(h[0].kind == "tool" for h in hits_new), f"更新后新内容必须可召回: {hits_new}"

    hits_old = store.search("甲甲甲唯一词", session_id="A")
    assert not any(h[0].kind == "tool" for h in hits_old), f"更新后旧内容不得残留召回: {hits_old}"

    hits_b = store.search("乙乙乙唯一词", session_id="A")
    assert not any(h[0].kind == "tool" for h in hits_b), f"A 会话不得召回 B 的内容: {hits_b}"


# ---------- FTS 查询运算符字符安全化 ----------


def test_contract_wildcard_query_recall_long_term() -> None:
    """契约（FTS 安全化·破坏者）：通配/版本号形态查询不得因 FTS5 运算符字符
    （`*.py` 语法错误、`-py` NOT）丢失召回——修复前 `*.py` 查询 FTS 失败→LIKE
    回退 0 命中（文件名类查询恒失效）。"""
    from bandi.memory.long_term import LongTermMemory, MemoryItem

    store = LongTermMemory(db_path=":memory:")
    store.store(MemoryItem(item_id="m1", content="脚本 main.py 工具说明", importance=0.5))
    hits = store.search_fts("*.py", limit=5)
    assert hits, "通配 .py 查询必须命中 main.py 内容（修复前空召回）"
    store.close()


def test_contract_wildcard_query_recall_working(tmp_path: Path) -> None:
    """契约（同族）：working 检索同款——`*.py` 形态查询必须命中 KV 快照。"""
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "sp.db"))
    store.set("k1", "脚本 main.py 工具说明乙", session_id="s")
    hits = store.search("*.py", session_id="s")
    assert hits, "通配 .py 查询必须命中（FTS 路径修复后）"
    store.close()


# ---------- 合并缓冲截断标记 ----------


def test_contract_merge_buffer_truncation_marked() -> None:
    """契约（静默丢内容·破坏者）：合并超 500 字符必须带截断标记——
    修复前硬截无标记（内容消失且不可见）。"""
    from bandi.core.message import Message, MessageRole
    from bandi.memory.compression_pipeline import _merge_message_buffer

    buffer = [
        Message(role=MessageRole.USER, content="甲" * 400),
        Message(role=MessageRole.USER, content="乙" * 400),
    ]
    merged = _merge_message_buffer(buffer)
    assert merged is not None
    assert len(merged.content) <= 500
    assert "..." in merged.content, f"截断必须带标记: {merged.content[-24:]!r}"


# ---------- markdown FTS 显式关闭 ----------


def test_contract_markdown_store_close(tmp_path: Path) -> None:
    """契约（资源可释放·破坏者）：MarkdownMemoryStore FTS 连接必须可显式关闭
    （其它 SQLite 资源均有 close 路径，此处此前缺失）。"""
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    store = MarkdownMemoryStore(tmp_path, tmp_path)
    store._fts()  # 建立连接（等价首次检索/重建路径）
    assert store._fts_conn is not None
    store.close()
    assert store._fts_conn is None, "close 后连接必须释放"
    store._fts()  # 惰性重开不得炸（关闭后可继续使用）


# ---------- episodic REPLACE 孤儿 FTS 清理 ----------


def test_contract_episodic_replace_no_orphan_fts(tmp_path: Path) -> None:
    """契约（索引不膨胀·破坏者）：episode_id 主键 REPLACE 换 rowid 后旧 FTS 行
    必须清理——修复前孤儿行残留（索引单调膨胀）。"""
    from bandi.memory.episodic import Episode, EpisodicStore

    store = EpisodicStore(str(tmp_path / "ep.db"))
    base = Episode(summary="第一版内容", episode_id="e1")
    store.record(base)
    store.record(replace(base, summary="第二版内容"))

    count = store._conn.execute("SELECT COUNT(*) FROM episodes_fts").fetchone()[0]
    assert count == 1, f"REPLACE 后旧 FTS 行必须清理（实际 {count} 行）"
    store.close()


# ---------- 旋转后续接不误报哈希链断裂 ----------


def test_contract_replay_after_rotation_not_broken(tmp_path: Path, monkeypatch) -> None:
    """契约（跨代链·破坏者）：rotate 后 replay 不得把续接首行（prev_hash 指向
    旧链尾）误报为哈希链断裂——修复前单文件校验恒报 hash_chain_broken。"""
    import asyncio

    import bandi.core.event_bus as ev_mod
    from bandi.core.event_bus import EventBus
    from bandi.core.events import Event, EventType

    monkeypatch.setattr(ev_mod, "_PERSIST_ROTATE_BYTES", 10)
    path = tmp_path / "events-r.jsonl"
    bus = EventBus(persist_path=path)
    bus.enable_persistence()
    asyncio.run(bus.emit(Event(type=EventType.LLM_REQUEST, data={"a": 1})))
    asyncio.run(bus.emit(Event(type=EventType.TURN_STARTED, data={"b": 2})))
    asyncio.run(bus.emit(Event(type=EventType.ERROR_OCCURRED, data={"c": 3})))
    asyncio.run(bus.emit(Event(type=EventType.ERROR_OCCURRED, data={"d": 4})))
    bus.disable_persistence()

    bus2 = EventBus(persist_path=path)
    report = bus2.replay(dry_run=True)
    assert report.hash_chain_broken is False, f"旋转续接不得误报断裂: {report.errors}"
    assert report.ok is True, f"续接+单调+唯一时 ok 必须为 True: {report.errors}"


# ---------- checkpoint 不得成为消息 parent ----------


def test_contract_session_leaf_skips_checkpoint(tmp_path: Path) -> None:
    """契约（会话链一致·破坏者）：checkpoint 导航条目不得成为消息 parent——
    修复前 latest leaf 不过滤 entry_type，fork/时间旅行路径偏移。"""
    from bandi.core.message import Message, MessageRole
    from bandi.memory.session.store import SessionStore

    store = SessionStore(tmp_path / "s.db")
    sid = store.create_session(title="t")
    m1 = store.append_message(sid, Message(role=MessageRole.USER, content="你好"))
    store.save_checkpoint_entry(sid, turn_index=1)
    m2 = store.append_message(sid, Message(role=MessageRole.ASSISTANT, content="回复"))

    entries = {e["id"]: e for e in store.get_branch(sid)}
    assert entries[m2]["parent_id"] == m1, (
        f"消息 parent 必须为上一条消息（不得是 checkpoint）: {entries[m2]['parent_id']}"
    )
    store.close()


# ---------- LLM 计时归属（并发不串扰） ----------


@pytest.mark.asyncio
async def test_contract_llm_duration_attribution_concurrent() -> None:
    """契约（计时归属·破坏者）：并发 LLM 请求 duration 不得串扰——修复前单标量
    _llm_start 使先发起（耗时更长）的请求采用后发起者的起点。"""
    import asyncio
    from types import SimpleNamespace

    from bandi.observability.bridge import MetricsBridge

    class _CaptureMetrics:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, float]] = []

        def record_llm_call(
            self,
            provider: str,
            model: str,
            status: str,
            prompt_tokens: int,
            completion_tokens: int,
            duration: float,
        ) -> None:
            self.calls.append((provider, model, duration))

    def _ev(data: dict) -> SimpleNamespace:
        return SimpleNamespace(data=data)

    cap = _CaptureMetrics()
    bridge = MetricsBridge(metrics=cap)  # type: ignore[arg-type]
    await bridge.on_llm_request(_ev({"provider": "pa", "model": "m", "session_id": "A"}))
    await asyncio.sleep(0.2)
    await bridge.on_llm_request(_ev({"provider": "pb", "model": "m", "session_id": "B"}))
    await asyncio.sleep(0.1)
    await bridge.on_llm_response(_ev({"provider": "pa", "model": "m", "session_id": "A"}))
    await asyncio.sleep(0.1)
    await bridge.on_llm_response(_ev({"provider": "pb", "model": "m", "session_id": "B"}))

    durations = {provider: duration for provider, _model, duration in cap.calls}
    assert 0.25 < durations["pa"] < 0.40, (
        f"A 实际耗时 ~0.30s 必须被如实记录（修复前被 B 的起点低估为 ~0.1s）: {durations}"
    )
    assert 0.15 < durations["pb"] < 0.35, (
        f"B 实际耗时 ~0.20s 必须被如实记录（修复前单标量被清空归零）: {durations}"
    )
