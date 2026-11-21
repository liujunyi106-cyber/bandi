"""功能：记忆巩固的 LLM 提炼必须是「有限耗时」，不得让一轮巩固拖垮主链。

真机缺陷（一个「搜 AI 新闻」任务耗时 31m55s，只发生 3 次 LLM 调用）：

    03:57:34  memory maintenance: promoted 13 episode(s)
    04:00:51  memory consolidation  (+197s)   ← 这 197 秒里主链零进展

`consolidate_episodes` 原实现在候选循环内逐条调用
`distill_for_episode` → `asyncio.run(distill_episode_async)` →
`accounted_complete(budget_sec=30)`：**串行**，每条最长等 30 秒。
候选多时整轮耗时 = 候选数 × 超时，期间共享事件循环的 Agent 主循环
排不到下一轮 LLM 调用（界面「正在执行」，后端零输出）。

契约钉住：
1. 多候选时必须并发提炼（不是串行累加）；
2. 整轮提炼受总预算约束，超出即回退规则提炼；
3. 判据不变——不该提名的候选一次 LLM 都不发起。
"""

from __future__ import annotations

import time

from bandi.memory import consolidation as cons


class _EpisodeStub:
    """功能：最小情节替身——只提供判据与提炼需要的字段。"""

    def __init__(
        self,
        episode_id: str,
        *,
        importance: float = 0.9,
        recall_count: int = 3,
        queries: tuple[str, ...] = ("q1", "q2"),
        summary: str = "用户要求把结算口径改成含税金额，最终确认按含税口径出报表。",
        tags: tuple[str, ...] = (),
        user_input: str = "",
    ) -> None:
        self.episode_id = episode_id
        self.importance = importance
        self.recall_count = recall_count
        self.recall_queries = list(queries)
        self.summary = summary
        self.tags = list(tags)
        self.user_input = user_input
        self.created_at = None
        self.project_id = None
        self.employee_id = None
        self.structured = None


class _LongTermStub:
    """功能：长期层替身——一律未晋升、无直写回合。"""

    def get(self, _item_id: str) -> None:
        return None

    def list_scopes(self) -> list[tuple[str | None, str | None]]:
        return [(None, None)]

    def list_items(self, **_kw: object) -> list[object]:
        return []


def test_candidates_are_distilled_concurrently() -> None:
    """功能：多候选提炼必须并发——串行会让耗时按候选数线性累加。"""
    seen: list[str] = []

    def _slow_distiller(episode: object) -> cons.DistilledMemory:
        seen.append(episode.episode_id)  # type: ignore[attr-defined]
        time.sleep(0.4)
        return cons.DistilledMemory(kind="fact", key="k", content="c", distilled_by="llm")

    episodes = [_EpisodeStub(f"e{i}") for i in range(4)]
    started = time.monotonic()
    results = cons._distill_candidates(
        episodes,
        long_term=_LongTermStub(),
        pending_ids=set(),
        distiller=_slow_distiller,
    )
    elapsed = time.monotonic() - started

    assert len(results) == 4, "每个候选都必须产出结果"
    assert elapsed < 0.4 * 4 * 0.75, (
        f"4 个候选耗时 {elapsed:.2f}s，接近串行的 {0.4 * 4:.1f}s——未并发执行"
    )


def test_round_budget_bounds_total_time() -> None:
    """功能：整轮提炼受总预算约束，慢候选不得无限拉长巩固。"""
    original = cons.DISTILL_ROUND_BUDGET_SEC
    cons.DISTILL_ROUND_BUDGET_SEC = 0.3

    def _very_slow(_episode: object) -> cons.DistilledMemory:
        time.sleep(2.0)
        return cons.DistilledMemory(kind="fact", key="k", content="c", distilled_by="llm")

    try:
        started = time.monotonic()
        cons._distill_candidates(
            [_EpisodeStub(f"e{i}") for i in range(3)],
            long_term=_LongTermStub(),
            pending_ids=set(),
            distiller=_very_slow,
        )
        elapsed = time.monotonic() - started
    finally:
        cons.DISTILL_ROUND_BUDGET_SEC = original

    assert elapsed < 3.0, f"总预算未生效，实际耗时 {elapsed:.1f}s"


def test_ineligible_candidates_never_called() -> None:
    """功能：判据不变——不该提名的候选一次 LLM 都不发起（防并发引入过度调用）。"""
    calls: list[str] = []

    def _counting(episode: object) -> cons.DistilledMemory:
        calls.append(episode.episode_id)  # type: ignore[attr-defined]
        return cons.DistilledMemory(kind="fact", key="k", content="c", distilled_by="llm")

    episodes = [
        _EpisodeStub("low", importance=0.1, recall_count=0),  # 低价值
        _EpisodeStub("rare", recall_count=0, importance=0.9),  # 召回不足
        _EpisodeStub("rejected", tags=(cons.PROMOTION_REJECTED_TAG,)),  # 用户已拒绝
        _EpisodeStub("ontology", tags=(cons.ONTOLOGY_TURN_TAG,)),  # 本体轮
        _EpisodeStub("ok"),  # 唯一合格
    ]
    results = cons._distill_candidates(
        episodes, long_term=_LongTermStub(), pending_ids=set(), distiller=_counting
    )

    assert calls == ["ok"], f"只有合格候选可提炼，实际调用了 {calls}"
    assert list(results) == ["ok"]


def test_pending_promotion_is_not_distilled_again() -> None:
    """功能：已在暂存区等批准的条目不得重复提炼（避免每轮改写暂存记录）。"""

    def _counting(_episode: object) -> cons.DistilledMemory:
        raise AssertionError("已暂存条目不该被提炼")

    results = cons._distill_candidates(
        [_EpisodeStub("e1")],
        long_term=_LongTermStub(),
        pending_ids={"consolidated:e1"},
        distiller=_counting,
    )
    assert results == {}
