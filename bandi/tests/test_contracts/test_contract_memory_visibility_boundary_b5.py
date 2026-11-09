"""契约族：B5 可见性边界。

行为不变式：
1. **前端可见面 = 通知（消息中心读 `/api/memory/pending`）+ 结构化草稿**——
 晋升草稿以 `[kind] 断言句` 摘要形态可见；不得裸情节原文包装。
2. **episodic 情节原文零前端出口**——全 `/api/memory/*` 端点响应不得携带
 情节原文（"用户: … → 回答: …" 包装与整段 transcript）；应用无 episodic 路由。
3. **批准闭环**——消息中心批准重放落 long_term；episodic 存储不被改动。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.memory.consolidation import consolidate_episodes
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import LongTermMemory

# 情节原文（独有词元 + 包装标签）：任何前端出口出现 = 边界破裂
RAW_TRANSCRIPT = (
    "用户: 请把 B5K7 的验收标准记下来 → 回答: 收到，B5K7 验收标准已记录并包含并发压测。"
)
WRAP_LABELS = ("用户:", "→ 回答:")


def _seed_high_value_episode(episodic: EpisodicStore, *, episode_id: str = "ep-b5-1") -> None:
    """高价值情节（importance 0.7 + recall 2）——走真实巩固提名路径。"""
    episodic.record(
        Episode(
            episode_id=episode_id,
            summary=RAW_TRANSCRIPT,
            importance=0.7,
            recall_count=2,
            recall_queries=["验收标准", "并发压测"],
        )
    )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """功能：真实 TestClient + 隔离的 episodic/long_term（审批重放目标 override）。"""
    import importlib

    from fastapi.testclient import TestClient

    from bandi.memory import staging as staging_mod
    from bandi.memory.markdown_memory import MarkdownMemoryStore

    app_mod = importlib.import_module("bandi.api.app")
    staging_mod.reset_pending_store(staging_mod.MemoryPendingStore(str(tmp_path / "pending")))
    episodic = EpisodicStore(str(tmp_path / "episodic.db"))
    long_term = LongTermMemory(str(tmp_path / "long_term.db"))
    markdown = MarkdownMemoryStore(tmp_path / "markdown")
    monkeypatch.setattr(
        app_mod,
        "_memory_replay_overrides",
        {"long_term": long_term, "episodic": episodic, "markdown": markdown},
    )
    monkeypatch.setattr(app_mod, "_API_KEY", "k-test")
    client = TestClient(app_mod.app)
    client.headers.update({"Authorization": "Bearer k-test"})
    yield SimpleNamespace(
        client=client, episodic=episodic, long_term=long_term, tmp=tmp_path, app_mod=app_mod
    )
    staging_mod.reset_pending_store()
    episodic.close()
    long_term.close()


def _all_memory_api_bodies(env: Any) -> dict[str, str]:
    """功能：调用全部 /api/memory/* 读端点，返回 name → 响应文本（负面断言输入）。"""
    bodies: dict[str, str] = {}
    for name, path in (
        ("events", "/api/memory/events"),
        ("pending", "/api/memory/pending"),
    ):
        resp = env.client.get(path)
        assert resp.status_code == 200, f"{path} 应可用：{resp.status_code} {resp.text[:200]}"
        bodies[name] = resp.text
    return bodies


def test_nominated_draft_visible_as_structured_preview(env: Any) -> None:
    """预期：提名草稿在消息中心可见，且摘要 = `[kind] 断言句` 结构化形态，不裸原文包装。"""
    from bandi.memory.distill import rule_distill

    _seed_high_value_episode(env.episodic)
    report = consolidate_episodes(
        env.episodic,
        env.long_term,
        distiller=lambda ep: rule_distill(ep.summary, importance=ep.importance),
    )
    assert report.staged == ["ep-b5-1"], f"高价值情节应进入暂存提名：{report.to_dict()}"

    resp = env.client.get("/api/memory/pending")
    rows = resp.json().get("pending") or []
    assert len(rows) == 1, f"消息中心应见 1 条草稿：{rows}"
    row = rows[0]
    assert row["action"] == "consolidate_episode"
    summary = str(row["summary"])
    assert summary.startswith("["), f"摘要应为结构化预览（[kind] 前缀）：{summary}"
    for label in WRAP_LABELS:
        assert label not in summary, f"摘要不得含情节原文包装「{label}」：{summary}"
    assert "B5K7" in summary, f"结构化内容应携带业务信息：{summary}"
    assert env.long_term.count() == 0, "未批准前不得落 long_term（草稿 ≠ 直写）"


def test_episodic_raw_transcript_never_reaches_any_memory_api(env: Any) -> None:
    """预期（负面，全端点）：提名后草稿可见，但情节原文 transcript 与包装标签零出现。"""
    from bandi.memory.distill import rule_distill

    _seed_high_value_episode(env.episodic)
    consolidate_episodes(
        env.episodic,
        env.long_term,
        distiller=lambda ep: rule_distill(ep.summary, importance=ep.importance),
    )
    bodies = _all_memory_api_bodies(env)
    for name, body in bodies.items():
        assert RAW_TRANSCRIPT not in body, f"{name} 泄漏情节原文 transcript"
        for label in WRAP_LABELS:
            assert label not in body, f"{name} 泄漏原文包装标签「{label}」"


def test_episodic_without_nomination_also_invisible(env: Any) -> None:
    """预期（负面）：未达提名门槛的情节（仅存 episodic）同样零前端出口。"""
    env.episodic.record(
        Episode(
            episode_id="ep-b5-low",
            summary=RAW_TRANSCRIPT,
            importance=0.3,
            recall_count=0,
        )
    )
    report = consolidate_episodes(env.episodic, env.long_term)
    assert report.staged == [], "低价值情节不得提名"
    bodies = _all_memory_api_bodies(env)
    for name, body in bodies.items():
        assert RAW_TRANSCRIPT not in body, f"{name} 泄漏未提名情节原文"
    assert env.client.get("/api/memory/pending").json().get("pending") == []


def test_no_episodic_frontend_route(env: Any) -> None:
    """预期（结构）：应用无任何 episodic 路由——情节层无直连出口。"""
    paths = [getattr(route, "path", "") for route in env.app_mod.app.routes]
    episodic_paths = [p for p in paths if "episodic" in p.lower()]
    assert episodic_paths == [], f"episodic 不得有前端路由：{episodic_paths}"


def test_approve_lands_long_term_and_episodic_untouched(env: Any) -> None:
    """预期（批准闭环）：消息中心批准 → 重放落 long_term；episodic 存储零改动。"""
    from bandi.memory.distill import rule_distill

    _seed_high_value_episode(env.episodic)
    consolidate_episodes(
        env.episodic,
        env.long_term,
        distiller=lambda ep: rule_distill(ep.summary, importance=ep.importance),
    )
    pending_id = env.client.get("/api/memory/pending").json()["pending"][0]["id"]

    resp = env.client.post(f"/api/memory/pending/{pending_id}/approve")
    assert resp.status_code == 200 and resp.json().get("approved") is True, resp.text
    assert env.long_term.get("consolidated:ep-b5-1") is not None, "批准应落 consolidated: 条目"
    assert env.client.get("/api/memory/pending").json().get("pending") == [], "批准后出队"

    episodes = env.episodic.list_recent(limit=10)
    assert len(episodes) == 1, "episodic 存储不得被批准动作改动"
    assert episodes[0].summary == RAW_TRANSCRIPT
