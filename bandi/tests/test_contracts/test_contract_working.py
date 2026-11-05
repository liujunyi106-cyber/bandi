"""
  KV 读写：ScratchpadStore.set/get（per-session 隔离 + TTL 惰性过期）
  工具历史：append_tool → tool_history 环形缓冲（上限内保留最新）
  检索分类：MemoryManager.search_all "working" 分类（session 过滤；未启用零新分类）
  注入产物：MemoryContextSource.collect 产出 [working] ContextItem（只含本 session）
  轮末写入：run_agent_turn → _persist_memory 自动记录任务快照 + 本轮工具调用
  关闭链：MemoryManager.close 级联关闭 working SQLite 连接

破坏者断言：同 key 跨 session 必须互不可见；TTL 过期后 get/search 全部不可见；
working=None 时 search_all 不得出现该分类（向后兼容）。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage


def _store(tmp_path: Any, **kwargs: Any) -> Any:
    from bandi.memory.working import ScratchpadStore

    return ScratchpadStore(str(tmp_path / "working.db"), **kwargs)


def _age(store: Any, key: str, session_id: str, days: float) -> None:
    """直接回写 created_at 制造「已存在 TTL 时长」的旧条目。"""
    past = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE scratch_kv SET created_at=? WHERE session_id=? AND key=?", (past, session_id, key)
    )
    conn.commit()
    conn.close()


# ── KV 读写 + session 隔离 + TTL ─────────────────────────────────────────


def test_kv_roundtrip_and_session_isolation(tmp_path: Any) -> None:
    """契约（P1a+破坏者）：set→get 读回一致；同 key 跨 session 互不可见。"""
    store = _store(tmp_path)
    store.set("turn:1", "用户: 排查 seed 报错 → 已给出补丁", session_id="sess-A")
    store.set("turn:1", "另一个会话的完全不同内容", session_id="sess-B")

    assert store.get("turn:1", session_id="sess-A") == "用户: 排查 seed 报错 → 已给出补丁"
    assert store.get("turn:1", session_id="sess-B") == "另一个会话的完全不同内容"
    assert store.get("turn:1", session_id="sess-C") is None, "未写入的 session 必须不可见"


def test_ttl_expiry_hides_entry_everywhere(tmp_path: Any) -> None:
    """契约（P1b+破坏者）：TTL 过期后 get/all/search/count 全部不可见（惰性清理）。"""
    store = _store(tmp_path, ttl_seconds=3600)
    store.set("ctx:db_url", "postgres://localhost/analytics", session_id="sess-1")
    _age(store, "ctx:db_url", "sess-1", days=1.0)

    assert store.get("ctx:db_url", session_id="sess-1") is None
    assert store.all(session_id="sess-1") == {}
    assert store.search("analytics 数据库", session_id="sess-1") == []
    assert store.count() == 0


def test_rejects_empty_key_or_value(tmp_path: Any) -> None:
    """契约（破坏者）：空 key/空 value 拒绝写入，count 不变。"""
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set("  ", "内容", session_id="s")
    with pytest.raises(ValueError):
        store.set("key", "   ", session_id="s")
    assert store.count() == 0


# ── 工具历史环形缓冲 ──────────────────────────────────────────────────────


def test_tool_history_ring_buffer_keeps_newest(tmp_path: Any) -> None:
    """契约：append_tool 超上限时淘汰最旧——history 只留最新 N 条且顺序保真。"""
    store = _store(tmp_path, tool_history_limit=3)
    for i in range(5):
        store.append_tool(
            __import__("bandi.memory.working", fromlist=["ToolInvocation"]).ToolInvocation(
                name=f"tool_{i}", success=True, result_preview=f"输出 {i}", session_id="s1"
            )
        )

    history = store.tool_history(session_id="s1")

    assert [t.name for t in history] == ["tool_4", "tool_3", "tool_2"], (
        f"必须只留最新 3 条且最新在前（实际 {[t.name for t in history]}）"
    )
    assert store.tool_history(session_id="s2") == [], "跨 session 历史必须隔离"


# ── search_all 分类 + 注入产物 ────────────────────────────────────────


def test_search_all_includes_working_and_filters_by_session(tmp_path: Any) -> None:
    """契约：search_all 返回 working 分类且 session_id 过滤生效；
    working=None 时无该分类（向后兼容）。"""
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "working.db"))
    store.set("turn:1", "Kubernetes 滚动更新参数排查结论", session_id="sess-1")
    manager = MemoryManager(
        long_term=LongTermMemory(str(tmp_path / "long.db")),
        episodic=None,
        working=store,
    )

    hit_session = manager.search_all("Kubernetes 滚动更新", limit=5, session_id="sess-1")
    miss_session = manager.search_all("Kubernetes 滚动更新", limit=5, session_id="sess-other")

    assert "working" in hit_session, f"必须含 working 分类（实际 {list(hit_session)}）"
    assert any("Kubernetes" in h.value for h in hit_session["working"])
    assert miss_session.get("working") == [], "其他 session 不得命中本 session 草稿"

    bare = MemoryManager(long_term=LongTermMemory(str(tmp_path / "long2.db")))
    results = bare.search_all("Kubernetes", limit=5, session_id="sess-1")
    assert "working" not in results, "未启用 working 时不得出现该分类"


def test_search_all_without_session_id_leaks_nothing(tmp_path: Any) -> None:
    """契约（P3b+破坏者）：调用方不传 session_id（旧签名兼容路径如 prefetch/测试桩）
    时 working 分类不得泄漏任何会话草稿（fail-closed 空桶隔离，非全局放行）。"""
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "working.db"))
    store.set("turn:1", "机密草稿内容不应跨参泄漏", session_id="sess-1")
    manager = MemoryManager(
        long_term=LongTermMemory(str(tmp_path / "long.db")),
        episodic=None,
        working=store,
    )

    results = manager.search_all("机密草稿", limit=5)

    assert results.get("working") == [], (
        f"不传 session_id 必须查空桶而非全量放行（实际 {results.get('working')}）"
    )


@pytest.mark.asyncio
async def test_context_source_collects_working_only_same_session(tmp_path: Any) -> None:
    """契约（注入侧最终产物）：collect 产出 [working] ContextItem
    且只注入当前 session 的草稿（跨 session 内容不得泄漏进上下文）。"""
    from bandi.context.sources.memory_source import MemoryContextSource
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "working.db"))
    store.set("turn:1", "恒星贸易发票抬头变更流程已确认", session_id="sess-mine")
    store.set("turn:1", "其他会话机密内容不应泄漏", session_id="sess-theirs")
    manager = MemoryManager(
        long_term=LongTermMemory(str(tmp_path / "long.db")),
        episodic=None,
        working=store,
    )

    source = MemoryContextSource(manager)
    items = await source.collect(
        Message(role=MessageRole.USER, content="恒星贸易 发票抬头 流程"), "sess-mine"
    )

    working_items = [i for i in items if i.id.startswith("memory:working:")]
    assert working_items, f"必须产出 working 注入项（实际 {[i.id for i in items]}）"
    joined = "\n".join(i.content for i in working_items)
    assert "恒星贸易" in joined
    assert "机密" not in joined, "跨 session 草稿严禁注入"


@pytest.mark.asyncio
async def test_run_agent_turn_persists_working_and_recalls_next_turn(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """
    工具调用历史 → 第二轮注入链召回 [working] 条目（跨轮草稿最终产物）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.session.store import SessionStore
    from bandi.memory.working import ScratchpadStore

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="已定位 seed 参数缺失问题并给出补丁方案",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )
    provider.add_response(
        LLMResponse(
            content="根据草稿继续处理",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 1
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="请排查 seed 参数缺失导致的启动崩溃",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
            r2 = await run_agent_turn(
                user_input="继续排查 seed 参数缺失问题",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )

        working_db = tmp_path / ".bandi" / "data" / "working.db"
        assert working_db.exists(), "轮末必须落盘 working.db"
        pad = ScratchpadStore(str(working_db))
        snapshots = pad.all(session_id=sid)
        assert any("seed" in v for v in snapshots.values()), (
            f"轮末任务快照必须落盘且含输入关键词（实际 {snapshots!r}）"
        )
        assert r2.reply, "第二轮必须有回复"

        from bandi.context.sources.memory_source import MemoryContextSource
        from bandi.memory.long_term import LongTermMemory
        from bandi.memory.memory_provider import MemoryManager

        manager = MemoryManager(
            long_term=LongTermMemory(str(tmp_path / "verify_long.db")), working=pad
        )
        injected = [
            item
            for item in await MemoryContextSource(manager).collect(
                Message(role=MessageRole.USER, content="seed 参数"), sid
            )
            if item.id.startswith("memory:working:")
        ]
        assert injected and any("seed" in i.content for i in injected), (
            "检索链必须能召回第一轮快照（跨轮闭环）"
        )
        store.close()


# ── 关闭链 ───────────────────────────────────────────────────────────────


def test_memory_manager_close_cascades_working(tmp_path: Any) -> None:
    """契约：MemoryManager.close 级联关闭 working 连接（关闭后操作抛错不静默）。"""
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    store = ScratchpadStore(str(tmp_path / "working.db"))
    manager = MemoryManager(long_term=LongTermMemory(str(tmp_path / "long.db")), working=store)

    manager.close()

    with pytest.raises(sqlite3.ProgrammingError):
        store.count()


def test_tool_result_error_recorded_in_history(tmp_path: Any) -> None:
    """契约（P2b）：失败工具调用的 error/duration_ms 保真落历史（复盘依据）。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    store.append_tool(
        ToolInvocation(
            name="bash",
            success=False,
            error="exit code 1: command not found",
            duration_ms=120,
            session_id="s1",
        )
    )

    (entry,) = store.tool_history(session_id="s1")
    assert entry.success is False
    assert entry.error == "exit code 1: command not found"
    assert entry.duration_ms == 120


def test_working_search_covers_error_text(tmp_path: Any) -> None:
    """m-fix 契约（P2c 破坏者）：工具失败原因必须可被检索——「上次 xx 为什么失败」
    仅凭 error 文案关键词必须命中（交付文档声称 preview/error 双扫，实现曾只扫 preview）。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    store.append_tool(
        ToolInvocation(
            name="git_commit",
            success=False,
            result_preview="",
            error="fatal: nothing to commit, working tree clean",
            session_id="s1",
        )
    )
    store.append_tool(
        ToolInvocation(
            name="web_search",
            success=True,
            result_preview="搜索结果正常返回",
            error=None,
            session_id="s1",
        )
    )

    hits = store.search("nothing to commit", session_id="s1")
    assert hits, "仅凭 error 文案关键词检索必须命中失败记录"
    assert hits[0][0].kind == "tool"
    assert "nothing to commit" in hits[0][0].value


def test_tool_history_dedupes_by_invocation_id(tmp_path: Any) -> None:
    """契约（P7a，审查修复）：同一 (session_id, invocation_id) 重复写入只保留最新一条——
    checkpoint 恢复/同 turn 重跑导致 append_tool 重复调用时不污染 tool history。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    store.append_tool(
        ToolInvocation(
            name="bash",
            success=False,
            error="exit 1: first attempt failed",
            result_preview="重跑前",
            session_id="s1",
            invocation_id="call_abc_1",
        )
    )
    store.append_tool(
        ToolInvocation(
            name="bash",
            success=True,
            error=None,
            result_preview="重跑后成功",
            session_id="s1",
            invocation_id="call_abc_1",
        )
    )

    history = store.tool_history(session_id="s1")
    assert len(history) == 1, f"同 invocation_id 重复写入必须去重（实际 {len(history)} 条）"
    assert history[0].success is True, "重复写入保留最新一次结果"
    assert "重跑后成功" in history[0].result_preview


def test_tool_history_different_invocation_ids_kept(tmp_path: Any) -> None:
    """契约（P7b 破坏者）：不同 invocation_id 必须各自保留——去重不得误伤
    同一 turn 内多次不同工具调用或不同 turn 的正常历史。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    for i, cid in enumerate(("call_a", "call_b", "call_c")):
        store.append_tool(
            ToolInvocation(
                name=f"tool_{i}",
                success=True,
                result_preview=f"输出 {i}",
                session_id="s1",
                invocation_id=cid,
            )
        )

    history = store.tool_history(session_id="s1")
    # 保留，且按调用先后倒序呈现（最新记录在列表头部）
    assert [t.invocation_id for t in history] == ["call_c", "call_b", "call_a"], (
        "不同 invocation_id 必须全部保留且最新在前"
    )


def test_tool_history_limit_returns_latest_records(tmp_path: Any) -> None:
    """契约（AR-21 修复的破坏者视角）：limit<记录数时必须返回最新 N 条
    而非 ASC 语义下的最旧 N 条（旧实现 ORDER BY id ASC LIMIT n 取早期记录）。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    for i, cid in enumerate(("call_1", "call_2", "call_3", "call_4", "call_5")):
        store.append_tool(
            ToolInvocation(
                name=f"tool_{i}",
                success=True,
                result_preview=f"输出 {i}",
                session_id="s1",
                invocation_id=cid,
            )
        )

    history = store.tool_history(session_id="s1", limit=3)
    assert [t.invocation_id for t in history] == ["call_5", "call_4", "call_3"], (
        "limit 必须取最新 N 条且最新在前"
    )


def test_tool_history_dedup_scoped_per_session(tmp_path: Any) -> None:
    """契约（P7c 破坏者）：去重按 (session_id, invocation_id) 作用域——同一
    invocation_id 跨 session 不得互相去重（不同会话独立历史）。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    for sid in ("s1", "s2"):
        store.append_tool(
            ToolInvocation(
                name="bash",
                success=True,
                result_preview=f"{sid} 的结果",
                session_id=sid,
                invocation_id="call_shared_1",
            )
        )

    assert len(store.tool_history(session_id="s1")) == 1
    assert len(store.tool_history(session_id="s2")) == 1
    assert "s1 的结果" in store.tool_history(session_id="s1")[0].result_preview
    assert "s2 的结果" in store.tool_history(session_id="s2")[0].result_preview


def test_tool_history_no_invocation_id_appends_always(tmp_path: Any) -> None:
    """契约（P7d 向后兼容 + 残余）：不传 invocation_id 时不同内容/不同工具
    仍按原追加语义各自入史——兜底幂等键只合并同工具同内容（见 残余契约）。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path, tool_history_limit=50)
    for i in range(3):
        store.append_tool(
            ToolInvocation(
                name=f"tool_{i}",
                success=True,
                result_preview=f"输出 {i}",
                session_id="s1",
            )
        )

    assert len(store.tool_history(session_id="s1")) == 3, (
        "无 invocation_id 时不同内容不得合并（原追加语义）"
    )


def test_tool_history_empty_invocation_id_dedupes_same_content(tmp_path: Any) -> None:
    """契约（残余）：空 tool_call_id 同会话同内容重复 append 只保留一条——
    LLM provider 不提供工具调用 id（或 checkpoint 恢复补写 working 通道）时
    同一次调用不得因重试产生重复历史。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    for _ in range(2):
        store.append_tool(
            ToolInvocation(
                name="bash",
                success=True,
                result_preview="相同结果",
                error=None,
                duration_ms=100,
                session_id="s1",
            )
        )

    history = store.tool_history(session_id="s1")
    assert len(history) == 1, f"空 id 同内容重复 append 必须去重（实际 {len(history)} 条）"
    assert history[0].result_preview == "相同结果"


def test_tool_history_empty_invocation_id_keeps_distinct_content(tmp_path: Any) -> None:
    """破坏者视角：空 id 兜底去重不得误伤——不同工具/不同结果/不同失败态各自保留。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    store.append_tool(
        ToolInvocation(name="bash", success=True, result_preview="结果甲", session_id="s1")
    )
    store.append_tool(
        ToolInvocation(name="bash", success=True, result_preview="结果乙", session_id="s1")
    )
    store.append_tool(
        ToolInvocation(name="git", success=True, result_preview="结果甲", session_id="s1")
    )
    store.append_tool(
        ToolInvocation(
            name="bash", success=False, error="exit 1", result_preview="结果甲", session_id="s1"
        )
    )

    assert len(store.tool_history(session_id="s1")) == 4, (
        "不同工具/不同结果/失败态的无 id 调用必须各自入史"
    )


def test_tool_history_empty_id_dedup_scoped_per_session(tmp_path: Any) -> None:
    """去重作用域仍按 (session_id, 兜底键)：不同会话不得互相去重。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    for sid in ("s1", "s2"):
        store.append_tool(
            ToolInvocation(name="bash", success=True, result_preview="同内容", session_id=sid)
        )

    assert len(store.tool_history(session_id="s1")) == 1
    assert len(store.tool_history(session_id="s2")) == 1


def test_legacy_schema_migrates_invocation_id_column(tmp_path: Any) -> None:
    """契约（P7e 防回归/迁移）：旧 schema 的 scratch_tools 无 invocation_id 列时
    ScratchpadStore 打开必须 ALTER 补列并建唯一索引——不得因 CREATE TABLE IF NOT
    不重建既有表而抛 no such column（生产已有库迁移崩溃根因）。"""
    from bandi.memory.working import ToolInvocation

    db = str(tmp_path / "legacy.db")
    from bandi.memory.working import ScratchpadStore

    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE scratch_tools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 1,
            result_preview TEXT NOT NULL DEFAULT '',
            error TEXT,
            duration_ms INTEGER,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "INSERT INTO scratch_tools (session_id, name, success, result_preview, created_at) "
        "VALUES ('s1', 'bash', 1, '存量历史', datetime('now'))"
    )
    conn.commit()
    conn.close()

    store = ScratchpadStore(db)  # 不得抛异常
    store.append_tool(
        ToolInvocation(
            name="git",
            success=True,
            result_preview="新记录",
            session_id="s1",
            invocation_id="call_new",
        )
    )

    history = store.tool_history(session_id="s1")
    assert len(history) == 2, "旧记录必须保留且新记录已带上 invocation_id 去重能力"
    assert any("存量历史" in h.result_preview for h in history), "旧库数据必须完整保留"


def _raw_db(db_path: str) -> Any:
    conn = sqlite3.connect(db_path)
    return conn


def test_fts_index_syncs_with_kv_and_tools(tmp_path: Any) -> None:
    """契约（P8a 存储产物）：写入后 FTS 索引与主表同步——KV 与工具历史各有一条
    可事实索引行（FTS 只是聚类召回路径，行为产物=索引一致可查）。"""
    from bandi.memory.working import ToolInvocation

    store = _store(tmp_path)
    store.set("ctx:plan", "任务计划：先修 seed 再升级数据库", session_id="s1")
    store.append_tool(
        ToolInvocation(
            name="bash",
            success=False,
            result_preview="fatal: nothing to commit",
            error="exit code 1",
            session_id="s1",
            invocation_id="call_fts_1",
        )
    )

    conn = _raw_db(store.db_path)
    kv_fts = conn.execute("SELECT COUNT(*) FROM scratch_kv_fts").fetchone()[0]
    tool_fts = conn.execute("SELECT COUNT(*) FROM scratch_tools_fts").fetchone()[0]
    conn.close()
    assert kv_fts == 1, "KV 写入后 FTS 必须有对应索引行"
    assert tool_fts == 1, "工具写入后 FTS 必须有对应索引行"


def test_legacy_schema_backfills_fts_on_open(tmp_path: Any) -> None:
    """契约（P8b 迁移）：存量库（已含主表数据、无 FTS 表）打开时 FTS 必须全量
    backfill——旧数据不重建索引即检索不可达，属于数据可见性回归。"""
    from bandi.memory.working import ScratchpadStore

    db = str(tmp_path / "legacy_fts.db")
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE scratch_kv (
            session_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (session_id, key)
        )"""
    )
    conn.execute(
        "INSERT INTO scratch_kv VALUES ('s1', 'ctx:legacy', '存量中文记忆片段', datetime('now'))"
    )
    conn.commit()
    conn.close()

    store = ScratchpadStore(db)
    hits = store.search("存量中文记忆", session_id="s1")
    assert any("存量中文记忆片段" in h.value for h, _ in hits), (
        "存量数据必须能经 FTS 召回（backfill）"
    )


def test_search_reaches_long_documents_with_chinese_substring(tmp_path: Any) -> None:
    """契约（P8c 长文档召回）：长文本中文子串查询必须命中——FTS 索引路径的最终产物。"""
    store = _store(tmp_path)
    body = "深度实践计划" + "长内容数据。" * 200
    store.set("ctx:deep", body, session_id="s1")

    hits, score = store.search("深度实践计划", session_id="s1")[0]
    assert score > 0, "中文子串查询必须有非零相关度"
    assert hits.value == body
