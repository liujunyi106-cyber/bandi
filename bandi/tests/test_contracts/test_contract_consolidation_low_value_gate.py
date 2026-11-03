# 功能：契约寒暄/自我介绍情节不得进入晋升候选
#
# 行为不变式（钉行为，不钉实现）：
#   1. 情节摘要的「回答侧」是自我介绍/能力清单 → 不晋升也不暂存（不产生审批噪音）
#   2. 业务内容情节（含用户侧寒暄但回答是实义内容）→ 照常晋升/暂存
#   3. 破坏者视角：recall_count 虚高的寒暄情节也不得因「高价值」信号放行
#   4. 空摘要不得崩溃，按跳过处理

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bandi.memory import consolidation


def _episode(summary: str, *, importance: float = 0.9, recall: int = 5) -> Any:
    """功能：构造情节替身（只含晋升判定所需字段）。"""
    return SimpleNamespace(
        episode_id="ep:test:1",
        summary=summary,
        importance=importance,
        recall_count=recall,
        recall_queries=["q1", "q2"],
        created_at="T00:00:00+00:00",
        project_id=None,
        employee_id=None,
        tags=[],
    )


class _FakeEpisodic:
    def __init__(self, episodes: list[Any]) -> None:
        self.episodes = episodes
        self.deleted: list[str] = []

    def list_recent(
        self, limit: int = 50, project_id: Any = None, employee_id: Any = None
    ) -> list[Any]:
        return list(self.episodes)

    def delete(self, episode_id: str) -> bool:
        self.deleted.append(episode_id)
        return True

    def search(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []


class _FakeLongTerm:
    def __init__(self) -> None:
        self.stored: list[Any] = []

    def store(self, item: Any, project_id: Any = None, employee_id: Any = None) -> None:
        self.stored.append(item)

    def get(self, item_id: str) -> Any:
        """功能：已晋升去重查询替身（固定返回 None = 未晋升过）。"""
        return None

    def list_scopes(self) -> list[Any]:
        return []


@pytest.fixture
def staged(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """功能：捕获 _stage_consolidation 调用（防写真实暂存目录）。"""
    calls: list[Any] = []
    monkeypatch.setattr(consolidation, "_stage_consolidation", lambda item, ep: calls.append(item))
    return calls


class TestLowValueEpisodeSkipped:
    def test_greeting_capability_episode_not_promoted(self, staged: list[Any]) -> None:
        """功能：核心不变式——问候+能力清单情节即使 recall 虚高也不进晋升候选。"""
        ep = _episode(
            "用户: 你好呀，你能帮我做什么？ → 回答: 老板好！我是 Bandi 助手，能帮您处理各类事务：检索、写作…"
        )
        long_term = _FakeLongTerm()

        report = consolidation.consolidate_episodes(_FakeEpisodic([ep]), long_term)

        assert staged == [], "自我介绍情节不得进暂存（审批噪音来源）"
        assert long_term.stored == []
        assert report.promoted == []

    def test_self_intro_only_episode_skipped(self, staged: list[Any]) -> None:
        """功能：纯自我介绍（无用户侧寒暄前缀）同样跳过。"""
        ep = _episode("回答: 我是 Bandi 助手，可以帮你检索、写作、处理文件。")

        consolidation.consolidate_episodes(_FakeEpisodic([ep]), _FakeLongTerm())

        assert staged == []

    def test_empty_summary_does_not_crash(self, staged: list[Any]) -> None:
        """功能：空摘要按跳过处理，不崩溃也不晋升。"""
        ep = _episode("")

        report = consolidation.consolidate_episodes(_FakeEpisodic([ep]), _FakeLongTerm())

        assert staged == []
        assert report.scanned == 1


class TestBusinessEpisodeStillPromoted:
    def test_business_conclusion_episode_staged(self, staged: list[Any]) -> None:
        """功能：业务结论情节照常走晋升（background 来源 → 暂存审批）。"""
        ep = _episode(
            "用户: 你好呀，帮我看看订单问题 → 回答: 结论：订单已锁定库存，原因是支付回调丢失，建议重放回调。"
        )

        consolidation.consolidate_episodes(_FakeEpisodic([ep]), _FakeLongTerm())

        assert len(staged) == 1, "业务情节必须保持晋升通道畅通"

    def test_answer_side_only_matters(self, staged: list[Any]) -> None:
        """功能：破坏者视角——用户侧寒暄不影响判定，只看回答侧。"""
        ep = _episode(
            "用户: 你好呀，你能帮我做什么？ → 回答: 结论：项目部署端口 7421，需要我把配置写进文档吗？"
        )

        consolidation.consolidate_episodes(_FakeEpisodic([ep]), _FakeLongTerm())

        assert len(staged) == 1
