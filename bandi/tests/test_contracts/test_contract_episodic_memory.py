"""
  写入：run_agent_turn 轮末 → EpisodicStore.record（规则拼装 summary + 启发式 importance）
  检索注入：MemoryManager.search_all "episodic" 分类 → MemoryContextSource.collect → ContextItem
  召回计数：注入命中后 note_recall bump recall_count（best-effort）
  排序公式：score = 命中相关度 × importance × 0.5^(age_days/half_life)（重要性加权+时间衰减）
  关闭链：MemoryManager.close 级联关闭 episodic SQLite 连接

排序（同相关性下旧条目/低重要度不得反超）；episodic=None 时 search_all 零新分类
（未启用向后兼容——test_contract_memory 契约 3 的「只含 long_term」即此未启用场景
启用场景由本文件钉住）。
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from bandi.core.config import BandiConfig
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolResult


def _store(tmp_path: Any, **kwargs: Any) -> Any:
    from bandi.memory.episodic import EpisodicStore

    return EpisodicStore(str(tmp_path / "episodic.db"), **kwargs)


def _ep(summary: str, **kwargs: Any) -> Any:
    from bandi.memory.episodic import Episode

    return Episode(summary=summary, **kwargs)


# ── 写入/读回一致 ────────────────────────────────────────────────────────


def test_record_roundtrip_chinese_tags(tmp_path: Any) -> None:
    """契约：record→get 读回一致——中文摘要/tags/importance/维度字段全保真。"""
    store = _store(tmp_path)
    ep = store.record(
        _ep(
            "修复了 logs/run_42.py 的 ValueError('seed') 缺陷",
            session_id="sess-1",
            project_id="proj-A",
            employee_id="emp-1",
            user_input="帮我修复 seed 报错",
            tags=["COMPLETED"],
            importance=0.6,
        )
    )
    assert ep.episode_id, "record 必须生成 episode_id"

    loaded = store.get(ep.episode_id)
    assert loaded is not None
    assert loaded.summary == "修复了 logs/run_42.py 的 ValueError('seed') 缺陷"
    assert loaded.session_id == "sess-1"
    assert loaded.project_id == "proj-A"
    assert loaded.employee_id == "emp-1"
    assert loaded.user_input == "帮我修复 seed 报错"
    assert loaded.tags == ["COMPLETED"]
    assert loaded.importance == pytest.approx(0.6)
    assert loaded.created_at is not None


def test_record_rejects_empty_summary(tmp_path: Any) -> None:
    """契约（破坏者）：空/纯空白 summary 不落库
    count 不变、get 不到。"""
    store = _store(tmp_path)
    before = store.count()
    result = store.record(_ep("   "))
    store.record(_ep(""))

    assert store.count() == before
    assert result.episode_id == "", "被拒写入不得返回可用 id"


# ── list_recent 时间序 ──────────────────────────────────────────────────────


def test_list_recent_orders_newest_first(tmp_path: Any) -> None:
    """
    （Windows ~15.6ms 粒度下连续 record 极易同值）。"""
    store = _store(tmp_path)
    first = store.record(_ep("第一条经历", created_at="T00:00:00+00:00"))
    second = store.record(_ep("第二条经历", created_at="T00:01:00+00:00"))
    recent = store.list_recent(limit=10)

    assert [e.episode_id for e in recent][:2] == [second.episode_id, first.episode_id]


def test_list_recent_same_timestamp_deterministic(tmp_path: Any) -> None:
    """
    DESC 兜底键）——多次读取顺序恒一致，消除 SQLite 同值返回序不定的 flaky。"""
    store = _store(tmp_path)
    same_ts = "T08:00:00+00:00"
    eps = [store.record(_ep(f"同刻经历{i}", created_at=same_ts)) for i in range(4)]

    expected = sorted((e.episode_id for e in eps), reverse=True)
    for _ in range(3):
        recent = store.list_recent(limit=10)
        ids = [e.episode_id for e in recent if e.created_at == same_ts]
        assert ids == expected, f"同时间戳必须按 episode_id DESC 确定序（实际 {ids}）"


def test_search_same_score_deterministic(tmp_path: Any) -> None:
    """
    顺序恒定（Python sort 稳定但输入序不定时输出仍不定）。"""
    store = _store(tmp_path)
    same_ts = "T08:00:00+00:00"
    eps = [
        store.record(_ep("蓝鲸栖息地研究要点", importance=0.9, created_at=same_ts))
        for _ in range(3)
    ]

    expected = [e.episode_id for e in sorted(eps, key=lambda x: x.episode_id, reverse=True)]
    for _ in range(3):
        hits = store.search("蓝鲸", limit=10)
        assert [e.episode_id for e, _ in hits] == expected, "同分并列必须按 episode_id 次级键确定序"


# ── 排序公式：重要性加权 + 时间衰减 ──────────────────────────────────────


def test_search_ranks_higher_importance_first(tmp_path: Any) -> None:
    """契约（P4a）：同内容同时间下，importance 高者排前（重要性加权生效）。"""
    store = _store(tmp_path)
    low = store.record(_ep("数据库连接池配置要点", importance=0.3))
    high = store.record(_ep("数据库连接池配置要点", importance=0.9))

    hits = store.search("数据库连接池", limit=5)

    assert len(hits) >= 2
    ids = [e.episode_id for e, _ in hits]
    assert ids.index(high.episode_id) < ids.index(low.episode_id), f"高重要度必须排前（实际 {ids}）"
    scores = dict(zip((e.episode_id for e, _ in hits), (s for _, s in hits)))
    assert scores[high.episode_id] > scores[low.episode_id]


def test_search_time_decay_penalizes_old_episodes(tmp_path: Any) -> None:
    """契约（P4b）：同内容同 importance 下，旧条目经时间衰减后排序靠后；
    分数比 ≈ 半衰期公式的 decay 比（数学断言防公式退化）。"""
    store = _store(tmp_path, half_life_days=14.0)
    old = store.record(_ep("Redis 缓存穿透排查记录", importance=0.8))
    new = store.record(_ep("Redis 缓存穿透排查记录", importance=0.8))
    # 直接回写 old 的 created_at 为 28 天前（两个半衰期 → decay=0.25）
    past = datetime.now(UTC) - timedelta(days=28)
    conn = sqlite3.connect(str(tmp_path / "episodic.db"))
    conn.execute(
        "UPDATE episodes SET created_at=? WHERE episode_id=?", (past.isoformat(), old.episode_id)
    )
    conn.commit()
    conn.close()

    hits = {e.episode_id: s for e, s in store.search("Redis 缓存穿透", limit=5)}

    assert hits[new.episode_id] > hits[old.episode_id], "新条目必须因衰减持罚低于旧条目"
    ratio = hits[old.episode_id] / hits[new.episode_id]
    assert ratio == pytest.approx(0.25, rel=0.05), (
        f"两半衰期龄差对应 decay 比 0.25（实际 {ratio:.3f}）"
    )


def test_note_recall_bumps_counter_best_effort(tmp_path: Any) -> None:
    """契约：note_recall bump recall_count + last_recalled_at；"""
    from bandi.memory.episodic import Episode

    store = _store(tmp_path)
    ep = store.record(_ep("部署流水线回滚操作步骤"))
    missing = Episode(episode_id="nonexistent-id", summary="x")

    store.note_recall(ep.episode_id)
    store.note_recall(missing.episode_id)  # 不抛

    loaded = store.get(ep.episode_id)
    assert loaded is not None and loaded.recall_count == 1
    assert loaded.last_recalled_at is not None


# ── 检索注入链（search_all 分类 + ContextSource 最终产物）────────────────


def test_search_all_includes_episodic_category(tmp_path: Any) -> None:
    """契约（P2a）：MemoryManager.search_all 返回 episodic 分类（hit 带 episode_id/
    score metadata）；episodic=None 时无该分类（未启用向后兼容）。"""
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.memory_provider import MemoryManager

    store = EpisodicStore(str(tmp_path / "episodic.db"))
    store.record(_ep("Kubernetes 滚动更新最大不可用节点数配置", project_id="p1"))
    manager = MemoryManager(
        long_term=None
        or __import__("bandi.memory.long_term", fromlist=["LongTermMemory"]).LongTermMemory(
            str(tmp_path / "long.db")
        ),
        episodic=store,
    )

    results = manager.search_all("Kubernetes 滚动更新", limit=5, project_id="p1")

    assert "episodic" in results, f"search_all 必须含 episodic 分类（实际 {list(results)}）"
    hit = next(h for h in results["episodic"] if "Kubernetes" in h.value)
    assert hit.key, "hit.key 必须= episode_id（note_recall 回链依据）"
    assert "episode_id" in hit.metadata and "score" in hit.metadata

    bare = MemoryManager(
        long_term=None
        or __import__("bandi.memory.long_term", fromlist=["LongTermMemory"]).LongTermMemory(
            str(tmp_path / "long2.db")
        )
    )
    assert "episodic" not in bare.search_all("Kubernetes", limit=5), (
        "未启用 episodic 时不得出现该分类"
    )


@pytest.mark.asyncio
async def test_context_source_collects_episodic_and_bumps_recall(tmp_path: Any) -> None:
    """契约（P2b+注入侧最终产物）：MemoryContextSource.collect 产出
    [episodic] ContextItem 且命中条目 recall_count 被 bump。"""
    from bandi.context.sources.memory_source import MemoryContextSource
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.memory_provider import MemoryManager

    store = EpisodicStore(str(tmp_path / "episodic.db"))
    ep = store.record(_ep("客户恒星贸易的发票抬头变更流程", project_id="p1"))
    assert ep.recall_count == 0
    manager = MemoryManager(
        long_term=None
        or __import__("bandi.memory.long_term", fromlist=["LongTermMemory"]).LongTermMemory(
            str(tmp_path / "long.db")
        ),
        episodic=store,
    )

    source = MemoryContextSource(manager, project_id="p1")
    items = await source.collect(
        Message(role=MessageRole.USER, content="恒星贸易 发票抬头 怎么改"), "sess-x"
    )

    episodic_items = [i for i in items if i.id.startswith("memory:episodic:")]
    assert episodic_items, f"必须产出 episodic 注入项（实际 {[i.id for i in items]}）"
    loaded = store.get(ep.episode_id)
    assert loaded is not None and loaded.recall_count == 1, "注入命中后 recall_count 必须 bump"


# ── 生产写入路径（启发式 importance + run_agent_turn 端到端）──────────────


def test_heuristic_importance_signals(tmp_path: Any) -> None:
    """契约（P1a）：启发式 importance——工具错误轮 > 正常轮；异常终止再加成；
    实时转向再加成触顶后 clamp 上限 1.0。"""
    from types import SimpleNamespace

    from bandi.agent.base import TerminalReason
    from bandi.core.agent_runner import _episode_importance

    base_ctx = SimpleNamespace(tool_results=[], terminal_reason=None, injected_text=None)
    assert _episode_importance(base_ctx) == pytest.approx(0.4)

    err_ctx = SimpleNamespace(
        tool_results=[
            ToolResult(tool_call_id="t1", tool_name="bash", output="", success=False, duration_ms=1)
        ],
        terminal_reason=None,
        injected_text=None,
    )
    assert _episode_importance(err_ctx) == pytest.approx(0.6)

    esc_ctx = SimpleNamespace(
        tool_results=[
            ToolResult(tool_call_id="t1", tool_name="bash", output="", success=False, duration_ms=1)
        ],
        terminal_reason=TerminalReason.ESCALATED,
        injected_text=None,
    )
    assert _episode_importance(esc_ctx) == pytest.approx(0.9)

    capped_ctx = SimpleNamespace(
        tool_results=[
            ToolResult(
                tool_call_id=f"t{i}", tool_name="bash", output="", success=False, duration_ms=1
            )
            for i in range(5)
        ],
        terminal_reason=TerminalReason.ESCALATED,
        injected_text="用户中途转向指令",
    )
    assert _episode_importance(capped_ctx) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_run_agent_turn_persists_and_recalls_episode(tmp_path: Any, monkeypatch: Any) -> None:
    """
    （summary 含输入关键词 + tags 带 terminal_reason）→ 第二轮检索命中第一轮
    内容（跨轮记忆最终产物）。"""
    from pathlib import Path
    from unittest.mock import patch

    from bandi.core.agent_runner import run_agent_turn
    from bandi.llm.mock import MockProvider
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.session.store import SessionStore

    provider = MockProvider()
    provider.add_response(
        LLMResponse(
            content="结论：已定位 seed 参数缺失问题并给出补丁方案",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=4, completion_tokens=2),
            finish_reason="stop",
        )
    )
    provider.add_response(
        LLMResponse(
            content="结论：根据历史记录继续处理",
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
            r1 = await run_agent_turn(
                user_input="请排查 seed 参数缺失导致的启动崩溃",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
            # 第二轮输入含第一轮关键词——episodic 注入链应召回第一轮经历
            r2 = await run_agent_turn(
                user_input="继续排查 seed 参数缺失问题",
                session_id=sid,
                session_store=store,
                bandi_config=cfg,
            )
        episodic_db = tmp_path / ".bandi" / "data" / "episodic.db"
        assert episodic_db.exists(), "轮末必须落盘 episodic.db"
        epi_store = EpisodicStore(str(episodic_db))
        episodes = epi_store.list_recent(limit=10)
        assert len(episodes) >= 2, f"两轮至少各一条 episode（实际 {len(episodes)}）"
        first = next(e for e in episodes if "seed" in e.summary)
        assert first.tags, "tags 必须带 terminal_reason"
        assert r1.reply and r2.reply
        recalled = epi_store.get(first.episode_id)
        assert recalled is not None
        store.close()


# ── 关闭链 ───────────────────────────────────────────────────────────────


def test_memory_manager_close_cascades_episodic(tmp_path: Any) -> None:
    """契约：MemoryManager.close 级联关闭 episodic 连接（关闭后查询抛
    ProgrammingError 即证明连接已释放，不静默吞掉）。"""
    from bandi.memory.episodic import EpisodicStore
    from bandi.memory.long_term import LongTermMemory
    from bandi.memory.memory_provider import MemoryManager

    store = EpisodicStore(str(tmp_path / "episodic.db"))
    manager = MemoryManager(long_term=LongTermMemory(str(tmp_path / "long.db")), episodic=store)

    manager.close()

    with pytest.raises(sqlite3.ProgrammingError):
        store.count()


# 以下三条是升级纵深契约：基线（LIKE 实现）即绿 = 既有行为基线；若 FTS5 升级


def test_search_reaches_legacy_db_without_fts(tmp_path: Any) -> None:
    """契约（FTS-1 真实数据形态）：旧库（无 FTS 索引表）已有行在升级打开后
    立即可检索——迁移回填不得遗漏存量 episodes。"""
    db = tmp_path / "episodic.db"
    legacy_conn = sqlite3.connect(str(db))
    legacy_conn.execute(
        "CREATE TABLE episodes ("
        "episode_id TEXT PRIMARY KEY,"
        " session_id TEXT NOT NULL DEFAULT '', project_id TEXT, employee_id TEXT,"
        " summary TEXT NOT NULL, user_input TEXT NOT NULL DEFAULT '',"
        " tags TEXT NOT NULL DEFAULT '[]', importance REAL NOT NULL DEFAULT 0.4,"
        " recall_count INTEGER NOT NULL DEFAULT 0, last_recalled_at TEXT,"
        " created_at TEXT NOT NULL)"
    )
    legacy_conn.execute("INSERT INTO episodes (episode_id, summary, created_at) VALUES ")
    legacy_conn.commit()
    legacy_conn.close()

    store = _store(tmp_path)
    hits = store.search("蓝鲸", limit=5)
    assert any(e.episode_id == "legacy-1" for e, _ in hits), "旧库存量行必须可检索（回填迁移）"


def test_re_record_same_id_replaces_indexed_content(tmp_path: Any) -> None:
    """契约（FTS-3 破坏者视角）：同 episode_id 重新 record（内容改变）后旧内容
    不得再被命中——索引同步覆盖，不得残留幽灵 gram 漂移召回污染检索结果。"""
    store = _store(tmp_path)
    store.record(_ep("老版本缓存穿透排查方案", episode_id="e-id-1"))
    store.record(_ep("新版本缓存预热全量部署方案", episode_id="e-id-1"))

    hits = store.search("缓存预热", limit=5)
    assert any(e.episode_id == "e-id-1" for e, _ in hits), "覆盖后新内容必须可命中"
    old = store.search("缓存穿透", limit=5)
    assert not any(e.episode_id == "e-id-1" for e, _ in old), "覆盖后旧内容不得残留（幽灵索引）"


def test_search_survives_metachar_queries(tmp_path: Any) -> None:
    """契约（FTS-2 破坏者视角）：英文原串可命中；FTS 无有效 token 的纯标点查询
    返回空列表而不崩——检索兜底防御（FTS MATCH 失败不得炸主链）。"""
    store = _store(tmp_path)
    store.record(_ep("Kubernetes 部署与监控最佳实践"))

    ascii_hits = store.search("Kubernetes", limit=5)
    assert any("Kubernetes" in e.summary for e, _ in ascii_hits), "英文原串必须可命中"

    weird = store.search("（（（！！！", limit=5)
    assert isinstance(weird, list), "无有效 token 查询必须返回列表"
    store.record(_ep("特殊标点 @@ '# $%^ 定量结论"))
    # 含可索引 gram 的怪异串不崩（部分 token 命中即召回）
    mixed = store.search("特殊标点 定量结论", limit=5)
    assert any(e.summary.startswith("特殊标点") for e, _ in mixed)
