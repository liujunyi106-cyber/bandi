"""契约：来源可靠性 + 四问路由。

生产调用路径：
1. LongTermMemory.note_feedback——metadata.reliability 默认 0.5，helpful +0.05 /
   unhelpful −0.10，clamp [0,1]；未命中返回 False
2. LongTermMemory.note_recalled——search_unified 仅对**真正返回**的 long_term 命中
   写回 recall_count/last_recalled_at（候选未返回不写）
3. _fusion_score——乘子 (0.85 + 0.15 × reliability) 作用于最终分 (base + intent_bias)；
   缺省 0.5 时全体 ×0.925 真正均匀（排序不变），有差异时才影响排序；显式 reliability
   差异 × intent 偏置 × question 倍率的组合顺序同样符合该公式
4. routing.classify_memory_query / route_bias——中文四问规则分类（task_state >
   history_conclusion > preference > project_background）与分类倍率；关键词须词组级
   命中（「背景音乐」不判），否定上下文按邻近窗口判定（「心理状态」不拖累同句
   「任务状态」），「上下文」归 project_background（「上下文中断」除外）
5. memory_source.collect 传 question_hint；POST /api/memory/feedback 端点复用单例目标

破坏者断言：
- 未返回候选不得写 recall_count
- 未知 item_id feedback 不落库（False/404）；损坏 metadata feedback → 409
- 空查询 / 未知 question_hint → 零行为（与缺省完全一致）
- 复合词子串不得误命中四问；feedback 每请求新建连接（构造计数 >1）即违约
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.memory_provider import MemoryManager, _fusion_score
from bandi.memory.routing import (
    MEMORY_QUESTIONS,
    classify_memory_query,
    route_bias,
)


class _StubLT:
    """确定性 long_term 替身——候选固定返回，忽略 limit（供截断破坏者用）。"""

    def __init__(self, items: list[MemoryItem], *, record_recalls: bool = False) -> None:
        self.items = items
        self.record_recalls = record_recalls
        self.recalled: list[str] = []

    def search_fts_ranked(self, query: str, limit: int = 5, **kwargs: Any) -> list[MemoryItem]:
        return list(self.items)

    def note_recalled(self, item_id: str) -> None:
        self.recalled.append(item_id)

    def count(self) -> int:
        return len(self.items)

    def close(self) -> None:
        pass


def _same_instant_rows(moment: datetime) -> list[MemoryItem]:
    return [
        MemoryItem(
            "lt1",
            "蓝鲸 长期事实",
            metadata={"_fts_rank": -1.0},
            timestamp=moment.timestamp(),
            importance=0.5,
        )
    ]


class _Working:
    def __init__(self, value: str, score: float, created_at: str) -> None:
        self._hit = (value, score, created_at)

    def search(self, query: str, limit: int = 5, session_id: str = "") -> list[Any]:
        from bandi.memory.working import ScratchHit

        value, score, created_at = self._hit
        return [(ScratchHit(kind="kv", key="w1", value=value, created_at=created_at), score)]

    def close(self) -> None:
        pass


# ── note_feedback：增量 / clamp / 未命中 ─────────────────────────────────────


def test_note_feedback_deltas_and_clamp(tmp_path: Any) -> None:
    """契约：默认 0.5；helpful +0.05、unhelpful −0.10；两端 clamp 到 [0,1]。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("f1", "需反馈的记忆"))

    assert lt.note_feedback("f1", helpful=True) is True
    assert lt.get("f1").metadata["reliability"] == pytest.approx(0.55)
    assert lt.note_feedback("f1", helpful=False) is True
    assert lt.get("f1").metadata["reliability"] == pytest.approx(0.45)

    for _ in range(30):
        lt.note_feedback("f1", helpful=True)
    assert lt.get("f1").metadata["reliability"] == pytest.approx(1.0), "helpful 上限 clamp 1.0"
    for _ in range(40):
        lt.note_feedback("f1", helpful=False)
    assert lt.get("f1").metadata["reliability"] == pytest.approx(0.0), "unhelpful 下限 clamp 0.0"
    lt.close()


def test_note_feedback_missing_item_returns_false(tmp_path: Any) -> None:
    """契约（破坏者）：未知 item_id 不落库、不新建、返回 False。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("exists", "已存在"))

    assert lt.note_feedback("no-such-id", helpful=True) is False
    assert lt.count() == 1 and lt.get("no-such-id") is None
    assert lt.get("exists").metadata.get("reliability") is None, "不得波及其他条目"
    lt.close()


def test_note_feedback_corrupt_metadata_raises_without_clobber(tmp_path: Any) -> None:
    """
    原值不得被覆盖。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("corrupt", "元数据损坏条目"))
    conn = sqlite3.connect(str(tmp_path / "lt.db"))
    conn.execute("UPDATE long_term_memory SET metadata = ? WHERE id = ?", ("{not-json", "corrupt"))
    conn.commit()
    conn.close()

    with pytest.raises(ValueError):
        lt.note_feedback("corrupt", helpful=True)

    conn = sqlite3.connect(str(tmp_path / "lt.db"))
    raw = conn.execute(
        "SELECT metadata FROM long_term_memory WHERE id = ?", ("corrupt",)
    ).fetchone()[0]
    conn.close()
    assert raw == "{not-json", "损坏元数据不得被反馈覆盖"
    lt.close()


# ── 融合可靠性因子 ───────────────────────────────────────────────────────────


def test_fusion_reliability_uniform_default_keeps_order() -> None:
    """契约：无 intent 偏置时，缺省 reliability=0.5 为均匀缩放（排序不变）；
    reliability=1 → 不缩放、0 → 0.85 下界。含 intent 偏置的排序级口径见
    test_fusion_reliability_scales_final_score_with_intent_bias。"""
    meta = {
        "score": 0.5,
        "importance": 0.5,
        "timestamp": datetime.now(UTC).isoformat(),
        "recall_count": 0,
    }
    assert _fusion_score(meta) == pytest.approx(_fusion_score({**meta, "reliability": 0.5}))

    high = {**meta, "reliability": 1.0}
    low = {**meta, "reliability": 0.0}
    base = _fusion_score(meta) / 0.925
    assert _fusion_score(high) == pytest.approx(base), "reliability=1 → 不缩放"
    assert _fusion_score(low) == pytest.approx(0.85 * base), "reliability=0 → 0.85 下界"


def test_search_unified_reliability_breaks_equal_score_tie(tmp_path: Any) -> None:
    """契约：其余信号完全相同时，高 reliability 命中排序在前。"""
    moment = datetime.now(UTC)
    rows = [
        MemoryItem(
            "lt-lo",
            "蓝鲸 低可靠内容",
            metadata={"_fts_rank": -1.0, "reliability": 0.1},
            timestamp=moment.timestamp(),
            importance=0.5,
        ),
        MemoryItem(
            "lt-hi",
            "蓝鲸 高可靠内容",
            metadata={"_fts_rank": -1.0, "reliability": 0.95},
            timestamp=moment.timestamp(),
            importance=0.5,
        ),
    ]
    mgr = MemoryManager(long_term=_StubLT(rows))

    out = mgr.search_unified("蓝鲸", limit=5)
    assert [h.key for h in out] == ["lt-hi", "lt-lo"], (
        f"同分下高 reliability 必须领先（实际 {[h.key for h in out]}）"
    )


def _rank_for_relevance(relevance: float) -> float:
    """功能：由目标相关度反推 FTS bm25 rank（_fts_relevance 的逆函数，测试独立构造）。"""
    return -relevance / (1.0 - relevance)


def _reliable_row(
    item_id: str, *, relevance: float, reliability: float, moment: datetime
) -> MemoryItem:
    """功能：构造可控相关度 + 显式 reliability 的 long_term 命中（同一时刻）。"""
    return MemoryItem(
        item_id,
        f"蓝鲸 长期事实 {item_id}",
        metadata={"_fts_rank": _rank_for_relevance(relevance), "reliability": reliability},
        timestamp=moment.timestamp(),
        importance=0.5,
    )


@pytest.mark.parametrize(
    ("intent_hint", "question_hint", "intent_bias", "question_bias"),
    [
        ("factual", "project_background", 0.05, 1.05),
        ("procedural", "project_background", 0.0, 1.05),
        ("factual", "history_conclusion", 0.05, 1.05),
    ],
)
def test_search_unified_reliability_question_intent_combined(
    intent_hint: str, question_hint: str, intent_bias: float, question_bias: float
) -> None:
    """契约：显式 reliability 0.9/0.1 与 intent 偏置、四问倍率并存时，最终顺序
    必须符合「(base + bias) × (0.85 + 0.15 × reliability) × question 倍率」——按公式
    手算低 base 高可靠项反超（base 差 ∈(0.046, 0.05)，reliability 因子才能翻盘）。"""
    moment = datetime.now(UTC)
    rows = [
        _reliable_row("lt-hi-unreliable", relevance=0.6, reliability=0.1, moment=moment),
        _reliable_row("lt-lo-reliable", relevance=0.4956, reliability=0.9, moment=moment),
    ]
    mgr = MemoryManager(long_term=_StubLT(rows))

    base_gap = 0.45 * (0.6 - 0.4956)
    assert 0.046 < base_gap < 0.05, "前置：两条命中 base 差必须落在 (0.046, 0.05)"

    base_hi = 0.45 * 0.6 + 0.2 * 0.5 + 0.25
    base_lo = 0.45 * 0.4956 + 0.2 * 0.5 + 0.25
    expected_hi = (base_hi + intent_bias) * (0.85 + 0.15 * 0.1) * question_bias
    expected_lo = (base_lo + intent_bias) * (0.85 + 0.15 * 0.9) * question_bias
    assert expected_lo > expected_hi, "前置：公式预测高可靠低 base 项反超"

    out = mgr.search_unified("蓝鲸", limit=5, intent_hint=intent_hint, question_hint=question_hint)
    assert [hit.key for hit in out] == ["lt-lo-reliable", "lt-hi-unreliable"], (
        f"reliability × question × intent 组合顺序必须符合公式（实际 {[hit.key for hit in out]}）"
    )


# ── note_recalled 写回 ───────────────────────────────────────────────────────


def test_note_recalled_writeback_readable_after_search(tmp_path: Any) -> None:
    """契约：search_unified 命中后 recall_count/last_recalled_at 可读回且逐次累加。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("q1", "蓝鲸栖息地研究记录", importance=0.9), "p1", "e1")
    mgr = MemoryManager(long_term=lt)

    out = mgr.search_unified("蓝鲸", project_id="p1", employee_id="e1")
    assert any(hit.key == "q1" for hit in out)

    refreshed = lt.get("q1")
    assert refreshed.metadata["recall_count"] == 1
    assert refreshed.metadata["last_recalled_at"]

    mgr.search_unified("蓝鲸", project_id="p1", employee_id="e1")
    assert lt.get("q1").metadata["recall_count"] == 2
    lt.close()


def test_note_recalled_only_for_returned_hits(tmp_path: Any) -> None:
    """契约（破坏者）：被 limit 截断而未返回的候选不得写 recall_count。"""
    moment = datetime.now(UTC)
    rows = [
        MemoryItem("top", "蓝鲸 首选内容", timestamp=moment.timestamp(), importance=0.9),
        MemoryItem("dropped", "蓝鲸 备选内容", timestamp=moment.timestamp(), importance=0.1),
    ]
    stub = _StubLT(rows, record_recalls=True)
    mgr = MemoryManager(long_term=stub)

    out = mgr.search_unified("蓝鲸", limit=1)

    assert [hit.key for hit in out] == ["top"]
    assert stub.recalled == ["top"], f"仅返回命中可写回（实际 {stub.recalled}）"


# ── 四问分类 / 倍率 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我的偏好吗", "preference"),
        ("我喜欢什么风格的回答", "preference"),
        ("这个项目的背景是什么", "project_background"),
        ("上次的结论是什么", "history_conclusion"),
        ("之前定过的方案是什么", "history_conclusion"),
        ("任务进度怎么样了", "task_state"),
        ("现在做到哪一步了", "task_state"),
        ("what is my preference", "preference"),
        ("task status please", "task_state"),
        ("项目背景和之前的结论都不重要，先看现在的进度", "task_state"),
        ("我喜欢什么风格，顺便说下项目背景", "preference"),
        ("背景音乐推荐", None),
        ("心理状态", None),
        ("上下文", "project_background"),
        ("帮我看看部署流程", None),
        ("随便聊聊天气", None),
        ("", None),
    ],
)
def test_classify_memory_query_chinese_rules(query: str, expected: str | None) -> None:
    """契约：中文四问规则分类（≥8 样例），冲突按 task_state > history_conclusion >
    preference > project_background；无命中 None。"""
    assert classify_memory_query(query) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("心理状态和任务状态都看看", "task_state"),
        ("心理状态", None),
        ("情绪状态怎么样", None),
        ("项目上下文", "project_background"),
        ("项目上下文中断", None),
    ],
)
def test_classify_memory_query_negation_window(query: str, expected: str | None) -> None:
    """契约：否定上下文按邻近窗口判定——「心理状态」不得拖累同句「任务状态」；
    「上下文中断」子串不得误命中 project_background。"""
    assert classify_memory_query(query) == expected


def test_route_bias_matrix_and_zero_behavior() -> None:
    """契约：四问各自返回固定分类倍率；未知/空 hint 返回空 dict（零行为）。"""
    allowed = {"long_term", "episodic", "working", "project_md"}
    assert set(MEMORY_QUESTIONS) == {
        "preference",
        "project_background",
        "history_conclusion",
        "task_state",
    }
    for question in MEMORY_QUESTIONS:
        bias = route_bias(question)
        assert bias, f"{question} 必须有分类倍率"
        assert set(bias) <= allowed
        assert all(value > 1.0 for value in bias.values())

    assert route_bias("task_state")["working"] == pytest.approx(1.2)
    assert route_bias("history_conclusion")["episodic"] == pytest.approx(1.15)
    assert route_bias("execute") == {} and route_bias("") == {} and route_bias(None) == {}


def test_search_unified_question_hint_reranks_category(tmp_path: Any) -> None:
    """契约：question_hint 命中四问时按分类倍率重排；None/未知 hint 与缺省完全一致。"""
    moment = datetime.now(UTC)
    lt = _StubLT(_same_instant_rows(moment))
    working = _Working("蓝鲸 任务草稿", 0.5, moment.isoformat())
    mgr = MemoryManager(long_term=lt, working=working)

    baseline = [hit.key for hit in mgr.search_unified("蓝鲸", limit=5)]
    assert baseline == ["lt1", "kv:w1"], "同分缺省按分类优先级破平（long_term 优先）"

    biased = [hit.key for hit in mgr.search_unified("蓝鲸", limit=5, question_hint="task_state")]
    assert biased == ["kv:w1", "lt1"], "task_state 倍率必须抬升 working 分类"

    assert [hit.key for hit in mgr.search_unified("蓝鲸", limit=5, question_hint=None)] == baseline
    assert [
        hit.key for hit in mgr.search_unified("蓝鲸", limit=5, question_hint="execute")
    ] == baseline, "非四问 hint 不得引入任何行为变化"


@pytest.mark.asyncio
async def test_memory_source_passes_question_hint_to_unified(monkeypatch: Any) -> None:
    """契约：生产链 collect 把（显式 intent_hint 或四问分类）作为 question_hint
    传给统一检索出口。"""
    from bandi.context.sources.memory_source import MemoryContextSource
    from bandi.core.message import Message, MessageRole

    captured: dict[str, Any] = {}

    class _Manager:
        def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
            captured.update(kwargs)
            captured["query"] = query
            return []

    source = MemoryContextSource(memory_manager=_Manager())
    await source.collect(Message(role=MessageRole.USER, content="我之前的结论是什么"), "s1")
    assert captured["question_hint"] == "history_conclusion", (
        f"无 intent_hint 时必须自动分类（实际 {captured.get('question_hint')}）"
    )

    captured.clear()
    await source.collect(
        Message(role=MessageRole.USER, content="我之前的结论是什么"),
        "s1",
        intent_hint="execute",
    )
    assert captured["question_hint"] == "history_conclusion", (
        "生产链 intent_hint 恒 plan/execute，四问分类不得被短路（修主链 FM-1）"
    )
    assert captured["intent_hint"] == "execute", "执行意图必须继续透传（plan/execute 偏置不丢）"


# ── feedback 端点 ────────────────────────────────────────────────────────────


@pytest.fixture()
def feedback_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """端点隔离：Path.home→tmp + 单例 override 双保险。"""
    import importlib

    app_module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(app_module, "_API_KEY", "test")
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    monkeypatch.setattr(app_module, "_memory_feedback_overrides", {"long_term": lt})
    yield lt
    lt.close()


def _feedback_client() -> TestClient:
    from bandi.api.app import app

    test_client = TestClient(app)
    test_client.headers["Authorization"] = "Bearer test"
    return test_client


def test_feedback_endpoint_roundtrip(feedback_env: Any) -> None:
    """契约：POST /api/memory/feedback 落 reliability，端到端可读回。"""
    feedback_env.store(MemoryItem("x", "反馈目标记忆"))
    response = _feedback_client().post(
        "/api/memory/feedback", json={"item_id": "x", "helpful": True}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"item_id": "x", "helpful": True}
    assert feedback_env.get("x").metadata["reliability"] == pytest.approx(0.55)


def test_feedback_endpoint_not_found_and_no_write(feedback_env: Any) -> None:
    """契约（破坏者）：未知 id → 404 统一错误体且不得建条目。"""
    response = _feedback_client().post(
        "/api/memory/feedback", json={"item_id": "missing", "helpful": False}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert feedback_env.get("missing") is None


def test_feedback_endpoint_invalid_payload_400(feedback_env: Any) -> None:
    """契约（破坏者）：缺 helpful / 非布尔 / 空 id → 400，不落任何写入。"""
    for payload in (
        {"item_id": "x"},
        {"item_id": "x", "helpful": "yes"},
        {"item_id": "", "helpful": True},
    ):
        response = _feedback_client().post("/api/memory/feedback", json=payload)
        assert response.status_code == 400, f"{payload} 必须 400（实际 {response.status_code}）"
    assert feedback_env.count() == 0


def test_feedback_endpoint_corrupt_metadata_conflict(feedback_env: Any) -> None:
    """契约（破坏者）：metadata 损坏 → 409 CONFLICT（不覆盖原值）。"""
    feedback_env.store(MemoryItem("corrupt", "损坏条目"))
    conn = sqlite3.connect(str(Path.home() / "lt.db"))
    conn.execute("UPDATE long_term_memory SET metadata = ? WHERE id = ?", ("{oops", "corrupt"))
    conn.commit()
    conn.close()

    response = _feedback_client().post(
        "/api/memory/feedback", json={"item_id": "corrupt", "helpful": True}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


def test_feedback_endpoint_reuses_single_target_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约：feedback 端点必须复用单例写入目标——两次请求同一实例（spy 构造
    计数 1）+ 端点行为不变，不得每请求新建连接并重跑 DDL。"""
    import importlib

    from bandi.memory.long_term import LongTermMemory as _RealLT

    app_module = importlib.import_module("bandi.api.app")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(app_module, "_API_KEY", "test")
    monkeypatch.setattr(app_module, "_memory_feedback_overrides", None)
    monkeypatch.setattr(app_module, "_memory_feedback_target", None, raising=False)

    constructed: list[int] = []

    class _SpyLT(_RealLT):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("bandi.memory.long_term.LongTermMemory", _SpyLT)
    client = _feedback_client()
    target: Any = None
    try:
        missing = client.post("/api/memory/feedback", json={"item_id": "x", "helpful": True})
        assert missing.status_code == 404, "前置：未知条目端点行为不变（404）"
        target = app_module._memory_feedback_target
        assert isinstance(target, _SpyLT), "默认路径必须经单例入口懒建目标"
        target.store(MemoryItem("x", "反馈目标记忆"))

        for _ in range(2):
            response = client.post("/api/memory/feedback", json={"item_id": "x", "helpful": True})
            assert response.status_code == 200, response.text
        assert len(constructed) == 1, f"两次请求必须复用同一实例（实际构造 {len(constructed)} 次）"
        assert target.get("x").metadata["reliability"] == pytest.approx(0.6)
    finally:
        if target is not None:
            target.close()


# ── 批量召回写回（单事务 + 同轮去重） ─────────────────────────────────────


class _CommitCounter:
    """commit 计数连接代理——不改事务语义，仅观测落库提交次数。"""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1
        self._conn.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _BatchStubLT:
    """带批量写回的 long_term 替身——记录批量调用参数。"""

    def __init__(self, items: list[MemoryItem]) -> None:
        self.items = items
        self.batches: list[list[str]] = []

    def search_fts_ranked(self, query: str, limit: int = 5, **kwargs: Any) -> list[MemoryItem]:
        return list(self.items)

    def note_recalled_many(self, item_ids: list[str]) -> None:
        self.batches.append(list(item_ids))

    def count(self) -> int:
        return len(self.items)

    def close(self) -> None:
        pass


def test_note_recalled_many_single_commit_and_dedup(tmp_path: Any) -> None:
    """契约：批量召回写回单事务（一次 commit）+ 同轮重复 id 去重。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    for i in range(5):
        lt.store(MemoryItem(f"r{i}", f"蓝鲸迁徙事实 {i}"), "p", "e")
    counter = _CommitCounter(lt._conn)
    lt._conn = counter

    lt.note_recalled_many(["r0", "r1", "r2", "r3", "r4", "r0"])

    assert counter.commits == 1, f"5 条命中必须一次事务（实际 {counter.commits} 次 commit）"
    for i in range(5):
        assert lt.get(f"r{i}").metadata["recall_count"] == 1, "重复 id 同轮只 +1"
    lt.close()


def test_search_unified_batches_unique_recall_writeback(tmp_path: Any) -> None:
    """契约：search_unified 汇总本轮唯一 long_term id 后批量写回（同 id 不重复）。"""
    moment = datetime.now(UTC)
    items = [
        MemoryItem(
            f"b{i}",
            f"蓝鲸批量召回条目 {i}",
            metadata={"_fts_rank": -1.0},
            timestamp=moment.timestamp(),
            importance=0.5,
        )
        for i in range(5)
    ]
    stub = _BatchStubLT(items)
    mgr = MemoryManager(long_term=stub)

    out = mgr.search_unified("蓝鲸", limit=5)

    assert len(out) == 5
    assert len(stub.batches) == 1, "必须批量调用一次"
    assert sorted(stub.batches[0]) == [f"b{i}" for i in range(5)]
    mgr._note_long_term_recalls([out[0], out[0]])
    assert stub.batches[-1] == [out[0].key], "同轮同 id 去重（set）"


# ── note_feedback 并发原子性 ──────────────────────────────────────────────


def test_note_feedback_concurrent_connections_no_lost_update(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约（破坏者）：两个连接并发 helpful×2 → 0.6（读改写不得丢更新）。"""
    import threading

    lt1 = LongTermMemory(str(tmp_path / "lt.db"))
    lt2 = LongTermMemory(str(tmp_path / "lt.db"))
    lt1.store(MemoryItem("f1", "并发反馈目标"), "p", "e")

    entered = threading.Event()
    release = threading.Event()
    fast_done = threading.Event()
    original = LongTermMemory._parse_metadata

    def _coordinated(raw: Any) -> dict:
        meta = original(raw)
        if threading.current_thread().name == "slow":
            entered.set()
            release.wait(timeout=10)
        return meta

    monkeypatch.setattr(LongTermMemory, "_parse_metadata", staticmethod(_coordinated))

    slow_result: list[bool] = []
    fast_result: list[bool] = []

    def _slow() -> None:
        slow_result.append(lt1.note_feedback("f1", helpful=True))

    def _fast() -> None:
        entered.wait(timeout=10)
        fast_result.append(lt2.note_feedback("f1", helpful=True))
        fast_done.set()

    slow = threading.Thread(target=_slow, name="slow")
    slow.start()
    fast = threading.Thread(target=_fast, name="fast")
    fast.start()
    # 修复后 fast 阻塞在 BEGIN IMMEDIATE（slow 持写锁）→ 此处超时；修复前立即完成
    fast_done.wait(timeout=1.0)
    release.set()
    slow.join(timeout=10)
    fast.join(timeout=10)

    assert slow_result == [True] and fast_result == [True]
    assert lt1.get("f1").metadata["reliability"] == pytest.approx(0.6), (
        "并发 helpful×2 必须都生效（丢失更新会停在 0.55）"
    )
    lt1.close()
    lt2.close()


# ── 乘子作用于最终分（含 intent 偏置） ────────────────────────────────────


def test_fusion_reliability_scales_final_score_with_intent_bias(tmp_path: Any) -> None:
    """契约：乘子必须作用于 (base + intent_bias) 之后的最终分——原实现偏置在乘子外
    缺省 reliability 不再是均匀缩放（高 base 无偏置项可被低 base 有偏置项反超）。"""
    from bandi.memory.working import ScratchHit

    moment = datetime.now(UTC)
    lt = _StubLT(
        [
            MemoryItem(
                "lt-a",
                "蓝鲸 长期事实",
                metadata={"_fts_rank": -1.0},
                timestamp=moment.timestamp(),
                importance=0.76,
            )
        ]
    )

    class _Working:
        def search(self, query: str, limit: int = 5, session_id: str = "") -> list[Any]:
            return [
                (
                    ScratchHit(
                        kind="kv", key="w1", value="蓝鲸 任务草稿", created_at=moment.isoformat()
                    ),
                    0.5,
                )
            ]

        def close(self) -> None:
            pass

    mgr = MemoryManager(long_term=lt, working=_Working())
    out = mgr.search_unified("蓝鲸", limit=5, intent_hint="procedural")
    assert [hit.key for hit in out] == ["lt-a", "kv:w1"], (
        f"缺省 factor 下 intent 偏置场景必须与无 factor 排序一致（实际 {[h.key for h in out]}）"
    )

    meta = {
        "score": 0.5,
        "importance": 0.5,
        "timestamp": moment.isoformat(),
        "recall_count": 0,
    }
    base = 0.575
    assert _fusion_score(meta, intent_bias=0.05) == pytest.approx((base + 0.05) * 0.925)
    assert _fusion_score({**meta, "reliability": 1.0}, intent_bias=0.05) == pytest.approx(
        base + 0.05
    )


# ── recall_count clamp / 坏值 timestamp 排序 ──────────────────────────


def test_note_recalled_clamps_negative_counts(tmp_path: Any) -> None:
    """契约（破坏者）：负 recall_count 必须 clamp ≥0（负值入库后 +1 不得仍为负）。"""
    lt = LongTermMemory(str(tmp_path / "lt.db"))
    lt.store(MemoryItem("neg", "负召回计数条目", metadata={"recall_count": -5}), "p", "e")
    lt.note_recalled("neg")
    assert lt.get("neg").metadata["recall_count"] == 1

    lt.store(MemoryItem("neg2", "负召回计数条目二", metadata={"recall_count": -3}), "p", "e")
    lt.note_recalled_many(["neg2"])
    assert lt.get("neg2").metadata["recall_count"] == 1
    lt.close()


def test_long_term_sorting_tolerates_non_numeric_timestamp(tmp_path: Any) -> None:
    """契约（破坏者）：TEXT/坏值 timestamp 不得让 decay 排序 TypeError——
    排序键统一 _as_float（坏值回退 0.0）且顺序稳定。"""
    import time

    lt = LongTermMemory(str(tmp_path / "lt.db"), decay_half_life_days=180.0)
    future = time.time() + 10_000.0
    lt.store(MemoryItem("t1", "蓝鲸迁移路线记录", timestamp=future), "p", "e1")
    lt.store(MemoryItem("t2", "蓝鲸迁移路线补充", timestamp=future + 1.0), "p", "e1")
    conn = sqlite3.connect(str(tmp_path / "lt.db"))
    conn.execute("UPDATE long_term_memory SET timestamp = 'abc' WHERE id = 't2'")
    conn.commit()
    conn.close()

    first = [item.id for item in lt.retrieve("蓝鲸", limit=5, project_id="p", employee_id="e1")]
    second = [item.id for item in lt.retrieve("蓝鲸", limit=5, project_id="p", employee_id="e1")]
    assert sorted(first) == ["t1", "t2"] and first == second, "坏值 timestamp 不得炸且排序稳定"

    ranked = [
        item.id for item in lt.search_fts_ranked("蓝鲸", limit=5, project_id="p", employee_id="e1")
    ]
    assert sorted(ranked) == ["t1", "t2"]
    lt.close()


# ── prefetch 四问分类透传 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prefetch_passes_question_hint_to_unified() -> None:
    """契约：prefetch 预取与 memory_source 并列独立分类四问，传 question_hint。"""
    from bandi.perception.prefetch import MemoryPrefetchProvider

    captured: dict[str, Any] = {}

    class _Manager:
        def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
            captured.update(kwargs)
            captured["query"] = query
            return []

    provider = MemoryPrefetchProvider(memory_manager=_Manager())
    try:
        await provider("我之前的结论是什么", "s1", [])
    finally:
        provider.close()

    assert captured["question_hint"] == "history_conclusion", (
        f"prefetch 必须传四问分类（实际 {captured.get('question_hint')}）"
    )
