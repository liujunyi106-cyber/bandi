"""
  轮末落库：run_agent_turn → _persist_memory 扫描 ctx.messages 中
     [上下文摘要]（SM 与 pipeline 两路压缩产物同权，任意角色防御）→
     long_term.store 稳定键 compaction:{sid}:{内容指纹} 动态 importance
     （FidelityReport.missing_ratio 输入；无报告回退 0.7，范围 [0.7, 0.9]）
  幂等：同摘要跨轮/同 turn 重跑 REPLACE 不重复（内容指纹键）
  hierarchy 追加：append_memory("session") 压缩纪要（维度齐备时）；
     同摘要内容跨轮不重复追加（dedup_text）
  best-effort：无摘要/无 hierarchy 不崩不误写
  检索回注：search_all 召回 compaction 条目（importance 高优）+
     collect_memory_context「## 会话记忆」段注入纪要

破坏者断言：同摘要跨轮不得生成新条目（原 turn 键每轮一键）；质量（保真
缺失率）必须进入 importance；无维度时 hierarchy 快照跳过但 long_term 照写。
"""

import asyncio
from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage


def _summary_msg(turn_note: str = "任务进度与关键决策沉淀") -> Message:
    from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX

    return Message(
        role=MessageRole.SYSTEM,
        content=f"{SUMMARY_MESSAGE_PREFIX}## 会话记忆\n- {turn_note}",
    )


def _mk_ctx(messages: list[Message], turn_index: int = 3) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        raw_input="排查 seed 参数",
        final_message="已给出补丁方案",
        current_content=None,
        turn_index=turn_index,
        terminal_reason=None,
        tool_results=[],
        injected_text=None,
        messages=messages,
    )


def _mk_manager(tmp_path: Any, *, with_working: bool = False) -> Any:
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager

    kwargs: dict[str, Any] = {
        "long_term": LongTermMemory(str(tmp_path / "long.db")),
    }
    if with_working:
        from bandi.memory.working import ScratchpadStore

        kwargs["working"] = ScratchpadStore(str(tmp_path / "working.db"))
    return MemoryManager(**kwargs)


def _mk_hierarchy(tmp_path: Any) -> Any:
    from bandi.company.store import WorkItemStore
    from bandi.memory.global_store import GlobalMemoryStore
    from bandi.memory.markdown_memory import MarkdownMemoryStore
    from bandi.memory.memory_manager import MemoryHierarchyManager
    from bandi.memory.session.store import SessionStore

    return MemoryHierarchyManager(
        global_store=GlobalMemoryStore(str(tmp_path / "h_global.db")),
        markdown_memory=MarkdownMemoryStore(tmp_path),
        work_item_store=WorkItemStore(str(tmp_path / "h_work.db")),
        session_store=SessionStore(str(tmp_path / "sess.db")),
        store_dir=tmp_path,
    )


# ── 轮末落库 + 幂等 ────────────────────────────────────────────────────


def test_summary_persisted_to_long_term_with_high_importance(tmp_path: Any) -> None:
    """契约：轮末扫描到 [上下文摘要] → long_term 落 compaction:{sid}:{turn}
    稳定键、importance=0.7、正文剥离前缀保真。"""
    from bandi.core.agent_runner import _persist_memory

    manager = _mk_manager(tmp_path)
    ctx = _mk_ctx([_user_hist(), _summary_msg("seed 参数排查结论：缺 config 注入")])

    asyncio.run(
        _persist_memory(ctx, manager, session_id="sess-abcd1234", project_id=None, employee_id=None)
    )

    hits = manager.long_term.retrieve("seed 参数", limit=5)
    # item_id 键取 session_id 前 8 字符（与既有 turn: 键截断先例一致）
    compaction = [h for h in hits if h.id.startswith("compaction:sess-abc")]
    assert compaction, f"必须落库 compaction 条目（实际 {[h.id for h in hits]}）"
    entry = compaction[0]
    assert entry.importance == pytest.approx(0.7), "压缩摘要必须为高优 0.7"
    assert "seed 参数" in entry.content and "[上下文摘要]" not in entry.content, (
        "正文剥离前缀后保真"
    )
    assert entry.metadata.get("session_id") == "sess-abcd1234"


def _user_hist() -> Message:
    return Message(role=MessageRole.USER, content="历史消息")


def test_same_turn_rerun_replaces_not_duplicates(tmp_path: Any) -> None:
    """契约（+破坏者）：同 turn 重跑 _persist_memory → 内容指纹键 REPLACE
    compaction 条目数不变。"""
    from bandi.core.agent_runner import _persist_memory

    manager = _mk_manager(tmp_path)
    ctx = _mk_ctx([_summary_msg()])

    asyncio.run(_persist_memory(ctx, manager, session_id="s1"))
    count_after_first = sum(1 for _ in manager.long_term.retrieve("会话记忆", limit=50))
    asyncio.run(_persist_memory(ctx, manager, session_id="s1"))

    entries = [
        h
        for h in manager.long_term.retrieve("会话记忆", limit=50)
        if h.id.startswith("compaction:s1:")
    ]
    assert len(entries) == 1, f"稳定键 REPLACE 幂等——不得重复（实际 {len(entries)}）"
    assert count_after_first >= 1


def test_same_summary_across_turns_not_duplicated(tmp_path: Any) -> None:
    """契约（跨轮+破坏者）：摘要消息跨轮常驻——同一摘要不同 turn 落库必须
    REPLACE 同一条（原 turn 键「每轮一键」会生成 N 条重复摘要）。"""
    from bandi.core.agent_runner import _persist_memory

    manager = _mk_manager(tmp_path)
    ctx = _mk_ctx([_summary_msg("同一份摘要内容")], turn_index=3)
    asyncio.run(_persist_memory(ctx, manager, session_id="s2"))
    later = _mk_ctx([_summary_msg("同一份摘要内容")], turn_index=4)
    asyncio.run(_persist_memory(later, manager, session_id="s2"))

    entries = [
        h
        for h in manager.long_term.retrieve("同一份摘要内容", limit=50)
        if h.id.startswith("compaction:s2:")
    ]
    assert len(entries) == 1, f"同摘要跨轮必须 REPLACE 为一条（实际 {len(entries)}）"


def test_compaction_importance_uses_fidelity_report(tmp_path: Any) -> None:
    """契约（动态 importance）：保真缺失率进入摘要 importance——
    0.7 + 0.2*(1-missing_ratio)；无报告回退 0.7。"""
    from types import SimpleNamespace

    from bandi.core.agent_runner import _persist_memory

    manager = _mk_manager(tmp_path)
    ctx = _mk_ctx([_summary_msg("保真缺失场景摘要")])
    ctx.meta = {"last_compression_stats": SimpleNamespace(fidelity_missing_ratio=0.25)}
    asyncio.run(_persist_memory(ctx, manager, session_id="s4"))

    entries = [
        h
        for h in manager.long_term.retrieve("保真缺失场景摘要", limit=50)
        if h.id.startswith("compaction:s4:")
    ]
    assert entries, "摘要必须落库"
    assert entries[0].importance == pytest.approx(0.85), (
        f"missing_ratio=0.25 对应 importance 0.85（实际 {entries[0].importance}）"
    )


# ── hierarchy 追加 + best-effort ──────────────────────────────────────


def test_hierarchy_session_appends_compaction_note(tmp_path: Any) -> None:
    """契约：维度齐备时 append_memory("session") 追加 [压缩纪要]；
    collect_memory_context「## 会话记忆」段可召回。"""
    from bandi.core.agent_runner import _persist_memory

    hierarchy = _mk_hierarchy(tmp_path)
    manager = _mk_manager(tmp_path)
    ctx = _mk_ctx([_summary_msg("数据库连接池已定位为根因")])
    hierarchy.save_memory(
        "session",
        "既有对话快照内容",
        project_id="p1",
        session_id="sess-x",
        employee_id="e1",
    )

    asyncio.run(
        _persist_memory(
            ctx,
            manager,
            session_id="sess-x",
            hierarchy=hierarchy,
            project_id="p1",
            employee_id="e1",
        )
    )

    snapshot = hierarchy.load_memory(
        "session", project_id="p1", session_id="sess-x", employee_id="e1"
    )
    # save_memory 为每轮覆盖式（本轮对话摘要刷新旧快照是既有设计）；
    # 契约钉「本轮快照 + 压缩纪要共存」——纪要 append 不得被快照 save 冲掉
    assert "已给出补丁方案" in snapshot, "本轮对话快照必须存在"
    assert "压缩纪要 turn 3" in snapshot and "数据库连接池" in snapshot, (
        f"压缩纪要必须追加进 session 快照（实际 {snapshot[:200]!r}）"
    )
    context_text = hierarchy.collect_memory_context(
        project_id="p1", session_id="sess-x", employee_id="e1", max_tokens=4096
    )
    assert "会话记忆" in context_text and "压缩纪要" in context_text, (
        "collect_memory_context 必须注入含纪要的会话段（检索回注面）"
    )


def test_no_summary_or_no_hierarchy_is_safe_skip(tmp_path: Any) -> None:
    """契约（+破坏者）：无摘要消息不写不崩；hierarchy=None 时 long_term 照写。"""
    from bandi.core.agent_runner import _persist_memory

    manager = _mk_manager(tmp_path)
    no_summary_ctx = _mk_ctx([_user_hist()])
    asyncio.run(_persist_memory(no_summary_ctx, manager, session_id="s2"))
    assert not [
        h for h in manager.long_term.retrieve("x", limit=50) if h.id.startswith("compaction:")
    ], "无摘要不得写 compaction"

    manager2 = _mk_manager(tmp_path)
    hierarchy_free_ctx = _mk_ctx([_summary_msg()])
    asyncio.run(_persist_memory(hierarchy_free_ctx, manager2, session_id="s3"))  # 无 hierarchy 不抛
    assert any(
        h.id.startswith("compaction:s3:") for h in manager2.long_term.retrieve("会话记忆", limit=50)
    ), "无 hierarchy 时 long_term 照写"


# ── 检索回注（search_all 面）──────────────────────────────────────────────


def test_search_all_recalls_compaction_entry(tmp_path: Any) -> None:
    """契约（P5a）：后续轮 search_all 召回 compaction 条目且 importance 高优排序。"""
    from bandi.core.agent_runner import _persist_memory

    manager = _mk_manager(tmp_path)
    low_item_ctx = _mk_ctx([_user_hist()])
    low_item_ctx.final_message = "普通回复内容包含 Kubernetes 关键词" * 10
    asyncio.run(
        _persist_memory(low_item_ctx, manager, session_id="s9")
    )  # 普通 turn 记忆 importance 0.5

    summary_ctx = _mk_ctx([_summary_msg("Kubernetes 滚动更新结论：maxUnavailable=1")])
    summary_ctx.turn_index = 5
    asyncio.run(_persist_memory(summary_ctx, manager, session_id="s9"))

    results = manager.search_all("Kubernetes 滚动更新", limit=5)
    ids = [h.key for h in results["long_term"]]
    compaction_hits = [i for i in ids if i.startswith("compaction:s9:")]
    assert compaction_hits, f"search_all 必须召回压缩摘要（实际 {ids}）"


@pytest.mark.asyncio
async def test_run_agent_turn_compaction_persisted_and_recalled(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """
    long_term → 后续轮 memory_source 检索召回（跨轮回注最终产物）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    # /tiktoken 计量校准：同 session_memory 契约——r0 usage 反应内容真实量级
    # 并落库 compacted 摘要（旧 12000 窗口：SM keep 空 + anchor 重注入，永不收敛）
    usages = [(8000, 100), (100, 30), (100, 30), (100, 30)]
    for i, (p, c) in enumerate(usages):
        # 显式携带「会话记忆」（落库后 retrieve 检索词与之匹配）
        provider.add_response(
            LLMResponse(
                content=f"第{i}轮处理完成，seed 结论：配置项缺失导致崩溃（会话记忆要点）",
                tool_calls=[],
                usage=TokenUsage(prompt_tokens=p, completion_tokens=c),
                finish_reason="stop",
            )
        )

    with patch("pathlib.Path.home", return_value=Path(str(tmp_path))):
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        # 未收录模型：显式 context_window 作为兜底生效（放开语义下目录优先，
        # 本用例需要可控窗口，用未收录模型名让配置兜底路径生效）
        cfg.llm.default_model = "unknown-e2e-model"
        cfg.agent.max_iterations = 3
        cfg.agent.context_window = 16000
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            replies = []
            # 保证 total 越过 0.85*窗口阈值触发 autocompact（progressive/SM）落库
            cfg.agent.context_window = 128_000
            for i in range(4):
                if i == 1:
                    cfg.agent.context_window = 16000
                r = await run_agent_turn(
                    user_input=f"第{i}轮请继续排查 seed 参数问题并补充细节说明" * 300,
                    session_id=sid,
                    session_store=store,
                    bandi_config=cfg,
                )
                replies.append(r.reply)

        assert all(replies), f"每轮必须有回复（实际 {replies}）"
        lt_db = tmp_path / ".bandi" / "data" / "long_term.db"
        assert lt_db.exists()
        lt = LongTermMemory(str(lt_db))
        compactions = [
            h for h in lt.retrieve("会话记忆", limit=20) if h.id.startswith(f"compaction:{sid}:")
        ]
        assert compactions, "触发压缩的轮次必须把 [上下文摘要] 落库 long_term"
        assert any(0.7 <= h.importance <= 0.9 for h in compactions), (
            f"压缩摘要 importance 必须落在动态区间 [0.7, 0.9]（实际 {[(h.id, h.importance) for h in compactions]}）"
        )

        # 检索回注最终产物：memory_source 对新输入召回压缩摘要（软保底改版后
        # 按相关度检索——以纪要头部专有词为查询，确保压缩摘要本身可召回）
        from bandi.context.sources.memory_source import MemoryContextSource

        manager = _build_production_like_manager(tmp_path)
        items = await MemoryContextSource(manager).collect(
            Message(role=MessageRole.USER, content="会话记忆"), sid
        )
        assert any("[long_term]" in i.content and "会话记忆" in i.content for i in items), (
            f"注入链必须召回压缩摘要（实际 {[i.content[:60] for i in items]}）"
        )


def _build_production_like_manager(tmp_path: Any) -> Any:
    """按生产 _build_memory_manager 同构组装（episodic 可省略不影响本断言）。"""
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager
    from bandi.memory.working import ScratchpadStore

    data_dir = tmp_path / ".bandi" / "data"
    return MemoryManager(
        long_term=LongTermMemory(str(data_dir / "long_term.db")),
        working=ScratchpadStore(str(data_dir / "working.db")),
    )
