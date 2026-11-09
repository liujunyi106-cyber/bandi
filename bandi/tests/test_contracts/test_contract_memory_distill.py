# 功能：契约——草稿提炼器（B2）：LLM 提炼 + 降级规则提炼 + 提名频次
#
# 行为不变式（钉行为，不钉实现）：
#   1. 提炼触发点 = 提名时：普通轮（未达门槛）零调用；高价值情节恰 1 次
#   2. LLM 成功 → 采用其 kind/key/content/importance（JSON 解析容忍围栏）
#   3. LLM 失败（异常/非法 JSON/非法 kind）→ 降级规则提炼，仍产出 kind/key
#   4. rule_distill 的 content 剥离「用户: X → 回答: Y」包装，取回答侧
#   5. 提炼绝不每轮对话调用（频次由提名门槛控制）

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.core.message import LLMResponse
from bandi.memory.distill import (
    DistilledMemory,
    distill_episode,
    distill_episode_async,
    rule_distill,
)
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import LongTermMemory
from bandi.memory.staging import MemoryPendingStore, reset_pending_store


class _FakeProvider:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.model_name = "fake-distill"

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        if self._error is not None:
            raise self._error
        return LLMResponse(content=self._content)


class TestRuleDistill:
    def test_strips_conversation_wrapper(self) -> None:
        memory = rule_distill(
            "用户: 我们项目的部署端口是 7421。 → 回答: 收到，端口 7421 已记录在案。",
            importance=0.4,
        )
        assert "→ 回答:" not in memory.content
        assert "用户:" not in memory.content
        assert "7421" in memory.content

    def test_fallback_kind_key_present(self) -> None:
        memory = rule_distill("这是一段普通的话。", importance=0.3)
        assert memory.kind in ("profile", "preference", "fact", "episode")
        assert memory.key, "kind/key 必存在（降级兜底）"
        assert memory.content

    def test_structured_embedded(self) -> None:
        memory = rule_distill("记住：我偏好把报表导出成 CSV 格式。", importance=0.4)
        data = memory.to_structured()
        assert data["kind"] == "preference"
        assert data["key"] == "format"


class TestDistillAsync:
    def test_llm_success_adopted(self) -> None:
        provider = _FakeProvider(
            content='{"kind": "preference", "key": "report_format", '
            '"content": "用户的报表导出偏好为 CSV 格式", "importance": 0.85}'
        )
        episode = Episode(
            episode_id="d1",
            summary="用户: 报表要 CSV。 → 回答: 好的。",
            importance=0.4,
        )
        memory = asyncio.run(distill_episode_async(episode, provider))
        assert memory.kind == "preference"
        assert memory.key == "report_format"
        assert memory.content == "用户的报表导出偏好为 CSV 格式"
        assert memory.importance == pytest.approx(0.85)
        assert memory.distilled_by == "llm"

    def test_invalid_json_falls_back(self) -> None:
        provider = _FakeProvider(content="抱歉我无法输出 JSON")
        episode = Episode(episode_id="d2", summary="用户: 记住。 → 回答: 好的。", importance=0.4)
        memory = asyncio.run(distill_episode_async(episode, provider))
        assert memory.distilled_by == "rule"
        assert memory.key

    def test_llm_error_falls_back(self) -> None:
        provider = _FakeProvider(error=RuntimeError("connection reset"))
        episode = Episode(
            episode_id="d3",
            summary="用户: 记住：端口是 7421。 → 回答: 已记录。",
            importance=0.4,
        )
        memory = asyncio.run(distill_episode_async(episode, provider))
        assert memory.distilled_by == "rule"
        assert memory.key

    def test_unknown_kind_falls_back(self) -> None:
        provider = _FakeProvider(
            content='{"kind": "banana", "key": "x", "content": "y", "importance": 0.5}'
        )
        episode = Episode(episode_id="d4", summary="用户: 记住。 → 回答: 好的。", importance=0.4)
        memory = asyncio.run(distill_episode_async(episode, provider))
        assert memory.kind in ("profile", "preference", "fact", "episode")
        assert memory.distilled_by == "rule"

    def test_empty_content_falls_back(self) -> None:
        provider = _FakeProvider(
            content='{"kind": "fact", "key": "port", "content": "", "importance": 0.5}'
        )
        episode = Episode(episode_id="d5", summary="用户: 记住。 → 回答: 好的。", importance=0.4)
        memory = asyncio.run(distill_episode_async(episode, provider))
        assert memory.content, "content 必非空"
        assert memory.distilled_by == "rule"


@pytest.fixture()
def pending_store(tmp_path):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


class TestNominationFrequency:
    def test_twenty_plain_rounds_zero_nomination_zero_calls(self, tmp_path, pending_store) -> None:
        from bandi.memory.consolidation import consolidate_episodes

        calls: list[str] = []
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        for i in range(20):
            episodic.record(
                Episode(
                    episode_id=f"plain-{i}",
                    summary=f"用户: 第 {i} 轮普通对话问题。 → 回答: 普通回答 {i}。",
                    importance=0.4,
                    recall_count=0,
                )
            )
        report = consolidate_episodes(
            episodic,
            long_term,
            distiller=lambda ep: calls.append(ep.episode_id) or rule_distill(ep.summary),
        )
        assert report.staged == [], "20 轮普通对话不得产生提名"
        assert pending_store.list_pending() == []
        assert calls == [], "未过门槛的情节不得调用提炼器（绝不每轮对话调用）"
        episodic.close()
        long_term.close()

    def test_high_value_round_one_nomination_one_call(self, tmp_path, pending_store) -> None:
        from bandi.memory.consolidation import consolidate_episodes

        calls: list[str] = []
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                episode_id="hv-1",
                summary="用户: 记住：报表统一导出 CSV。 → 回答: 已记录。",
                importance=0.85,
                recall_count=2,
            )
        )
        report = consolidate_episodes(
            episodic,
            long_term,
            distiller=lambda ep: calls.append(ep.episode_id) or rule_distill(ep.summary),
        )
        assert report.staged == ["hv-1"]
        assert calls == ["hv-1"], "高价值情节恰一次提炼调用"
        episodic.close()
        long_term.close()

    def test_unprocessed_sentinel_not_staged(self, tmp_path, pending_store) -> None:
        """提炼器明确返回“提炼不出”哨兵时，不得进入审批队列。"""
        from bandi.memory.consolidation import consolidate_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                episode_id="sentinel-1",
                summary="用户: 记住：报表统一导出 CSV。 → 回答: 已记录。",
                importance=0.85,
                recall_count=2,
            )
        )
        report = consolidate_episodes(
            episodic,
            long_term,
            distiller=lambda ep: DistilledMemory(
                kind="episode",
                key="unprocessed",
                content="一句话概括这段经历",
                importance=0.9,
                distilled_by="llm",
            ),
        )
        assert report.staged == []
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()


class TestDistilledMemoryShape:
    def test_defaults(self) -> None:
        memory = DistilledMemory(kind="fact", key="port", content="端口是 7421")
        assert memory.distilled_by == "rule"
        data = memory.to_structured()
        assert data == {"kind": "fact", "key": "port"}


class TestSyncFacadeProviderLifecycle:
    def test_fresh_provider_per_call(self, monkeypatch) -> None:
        """每次同步提炼独立构建 provider——跨 asyncio.run 复用会在绑定旧
        事件循环的 HTTP 客户端上报「Event loop is closed」。"""
        from bandi.memory import distill as distill_mod

        builds: list[int] = []
        fake = _FakeProvider(
            content='{"kind": "fact", "key": "port", "content": "端口 7421", "importance": 0.5}'
        )

        def _build() -> Any:
            builds.append(1)
            return fake

        monkeypatch.setattr(distill_mod, "_build_default_provider", _build)
        episode = Episode(
            episode_id="lc-1", summary="用户: 记住端口。 → 回答: 好的。", importance=0.4
        )
        first = distill_episode(episode)
        second = distill_episode(episode)
        assert len(builds) == 2, "不得跨调用复用 provider"
        assert first.key == "port" and second.key == "port"
