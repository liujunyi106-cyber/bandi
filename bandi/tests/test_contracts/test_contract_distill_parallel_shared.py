"""功能：记忆后台路径（维护晋升 + 巩固）的 LLM 提炼必须并发且有预算。

真机缺陷（同一个根因的两处实例）：

    consolidation:  197s  promote 阶段主链零进展（晋升 13 条）
    maintenance:     17s  promoted 14 episode(s)（晋升 14 条 = 14 次串行提炼）

两处都在 `for episode in episodes` 里逐条 `distill_for_episode`，
而它内部是 `asyncio.run(accounted_complete(budget_sec=30))`——
串行执行时整轮耗时 = 条数 × 超时，期间共享事件循环的 Agent 主循环
排不到下一轮 LLM 调用（界面「正在执行」，后端零输出）。

修复只改 consolidation 时漏了 maintenance 这个同族实例，
所以契约钉在**共享函数**上，两个调用方都要满足。
"""

from __future__ import annotations

import time

from bandi.memory import consolidation as cons


class _EpisodeStub:
    """功能：最小情节替身——提炼器只需要 episode_id 与 summary。"""

    def __init__(self, episode_id: str, summary: str = "用户确认按含税口径出报表。") -> None:
        self.episode_id = episode_id
        self.summary = summary
        self.importance = 0.9
        self.recall_count = 3
        self.recall_queries = ["q1", "q2"]
        self.tags: list[str] = []
        self.user_input = ""
        self.created_at = None
        self.project_id = None
        self.employee_id = None


def _distilled() -> cons.DistilledMemory:
    return cons.DistilledMemory(kind="fact", key="k", content="c", distilled_by="llm")


def test_parallel_helper_takes_less_than_serial_time() -> None:
    """功能：多候选必须并发——串行耗时按条数线性累加。"""

    def _slow(_episode: object) -> cons.DistilledMemory:
        time.sleep(0.4)
        return _distilled()

    episodes = [_EpisodeStub(f"e{i}") for i in range(4)]
    started = time.monotonic()
    results = cons.distill_episodes_parallel(episodes, distiller=_slow)
    elapsed = time.monotonic() - started

    assert len(results) == 4
    assert elapsed < 0.4 * 4 * 0.75, (
        f"4 条耗时 {elapsed:.2f}s，接近串行的 {0.4 * 4:.1f}s——未并发"
    )


def test_parallel_helper_respects_budget() -> None:
    """功能：总预算约束整轮耗时，慢提炼不得无限拉长。"""

    def _very_slow(_episode: object) -> cons.DistilledMemory:
        time.sleep(2.0)
        return _distilled()

    started = time.monotonic()
    cons.distill_episodes_parallel(
        [_EpisodeStub(f"e{i}") for i in range(3)], distiller=_very_slow, budget_sec=0.3
    )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"预算未生效，实际 {elapsed:.1f}s"


def test_single_failure_does_not_drop_others() -> None:
    """功能：单条提炼抛错不影响其余候选，且失败那条降级规则提炼而非丢失。"""

    def _flaky(episode: object) -> cons.DistilledMemory:
        if episode.episode_id == "bad":  # type: ignore[attr-defined]
            raise RuntimeError("llm down")
        return _distilled()

    results = cons.distill_episodes_parallel(
        [_EpisodeStub("ok1"), _EpisodeStub("bad"), _EpisodeStub("ok2")], distiller=_flaky
    )
    # distill_for_episode 内部捕获异常并降级规则提炼：不能因 LLM 失败就丢候选
    assert {"ok1", "ok2"} <= set(results), "其余候选不得受影响"
    assert "bad" in results, "失败候选必须降级规则提炼，不得静默丢弃"
    assert results["bad"].distilled_by != "llm", "降级结果不应冒充 LLM 提炼"


def test_empty_input_is_noop() -> None:
    """功能：无候选时不得发起任何提炼。"""

    def _must_not_call(_episode: object) -> cons.DistilledMemory:
        raise AssertionError("空输入不该调用提炼器")

    assert cons.distill_episodes_parallel([], distiller=_must_not_call) == {}


def test_maintenance_distills_concurrently() -> None:
    """功能：maintenance 晋升必须真的并发提炼（行为断言，不看源码字面量）。

    只改 consolidation 会留下 maintenance 这条同族串行路径
    （真机：maintenance 晋升 14 条 → 17s，主链全程零进展）。
    """
    from bandi.memory import maintenance as maint

    def _slow(_episode: object) -> cons.DistilledMemory:
        time.sleep(0.4)
        return _distilled()

    episodes = [_EpisodeStub(f"e{i}") for i in range(4)]
    started = time.monotonic()
    # 用替身长期层吞掉落库；本用例只量提炼阶段的耗时形状
    staged = maint._promote_to_long_term(_LongTermSink(), episodes, distiller=_slow)
    elapsed = time.monotonic() - started
    assert isinstance(staged, list)
    assert elapsed < 0.4 * 4 * 0.75, (
        f"maintenance 晋升 4 条耗时 {elapsed:.2f}s，接近串行的 {0.4 * 4:.1f}s——仍在逐条提炼"
    )


class _LongTermSink:
    """功能：长期层替身——吞掉 store()，本用例只关心提炼耗时。"""

    def store(self, *_args: object, **_kw: object) -> None:
        return None
