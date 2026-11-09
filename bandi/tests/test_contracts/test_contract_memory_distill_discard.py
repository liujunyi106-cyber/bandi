"""契约：降级提炼不得搬运助手回复，且必须能判「不值得记」。

缺陷实证：`rule_distill` 取「→ 回答:」之后的助手回复原文当
长期记忆内容，于是审批卡出现「[事实] 做好了老板！待办列表 App 已生成：预览路径…」；
LLM 路径又被强制「必须产出一条记忆」，把 ping/pong 写成经历。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bandi.core.message import LLMResponse
from bandi.memory.distill import (
    DistilledMemory,
    distill_episode_async,
    is_discard,
    rule_distill,
)
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import LongTermMemory
from bandi.memory.staging import MemoryPendingStore, reset_pending_store


class _FakeProvider:
    def __init__(self, content: str) -> None:
        self._content = content
        self.model_name = "fake-discard"

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return LLMResponse(content=self._content)


class TestRuleNeverCopiesAnswer:
    def test_answer_side_only_text_not_used(self) -> None:
        """回答侧独有内容（执行汇报）不得出现在记忆内容里。"""
        memory = rule_distill(
            "用户: 帮我做个待办 App。 → 回答: 做好了老板！App 已生成：预览路径：canvas/todo.html 功能：- 添加 - 删除",
            importance=0.5,
        )
        assert "预览路径" not in memory.content, f"搬运了回答侧：{memory.content}"
        assert "已生成" not in memory.content, f"搬运了回答侧：{memory.content}"
        assert "待办" in memory.content, f"用户侧内容丢失：{memory.content}"

    def test_answer_only_episode_discarded(self) -> None:
        """用户侧为空（只有回答）→ 无可记内容，判丢弃。"""
        memory = rule_distill("用户:  → 回答: 已生成：预览路径：canvas/todo.html", importance=0.5)
        assert is_discard(memory), f"应丢弃，实际 kind={memory.kind} content={memory.content!r}"

    def test_noisy_user_side_discarded(self) -> None:
        """用户侧本身是噪声（探针/寒暄）→ 丢弃，不产出记忆。"""
        assert is_discard(rule_distill("用户: ping → 回答: 老板，pong。一切正常。"))
        assert is_discard(rule_distill("用户: 你好 → 回答: 老板好！我是产品设计师。"))

    def test_chitchat_plus_completion_report_discarded(self) -> None:
        """用户寒暄 + 回答是执行汇报 → 丢弃（回答侧只在带结论信号时才被采用）。"""
        memory = rule_distill(
            "用户: 你好 → 回答: 做好了老板！已生成：预览路径：canvas/x.html 功能：- 输入 - 删除"
        )
        assert is_discard(memory), (
            f"执行汇报被当记忆：kind={memory.kind} content={memory.content!r}"
        )

    def test_chitchat_plus_conclusion_answer_kept(self) -> None:
        """用户寒暄 + 回答是实质结论 → 保留（助手查出的关键事实不丢）。"""
        memory = rule_distill("用户: 你好 → 回答: 结论：部署端口是 7421。")
        assert not is_discard(memory)
        assert "7421" in memory.content

    def test_legitimate_user_fact_kept(self) -> None:
        """破坏者视角：合法用户事实必须留下，不能被丢弃逻辑误杀。"""
        memory = rule_distill(
            "用户: 记住：我偏好报表统一导出 CSV 格式。 → 回答: 已记录。", importance=0.7
        )
        assert not is_discard(memory)
        assert "CSV" in memory.content
        assert memory.to_structured()["kind"] == "preference"


class TestLlmDiscardChannel:
    def test_llm_discard_short_circuits(self) -> None:
        """LLM 明确说 discard → 不产出记忆（此前被逼着编一条「经历」）。"""
        provider = _FakeProvider(content='{"kind": "discard"}')
        episode = Episode(episode_id="p1", summary="用户: ping → 回答: pong", importance=0.6)
        memory = asyncio.run(distill_episode_async(episode, provider))
        assert is_discard(memory)
        assert memory.distilled_by == "llm"
        assert memory.content == ""


@pytest.fixture()
def pending_store(tmp_path):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _discarding_distiller(episode: Episode) -> DistilledMemory:
    return DistilledMemory(kind="discard", key="", content="", distilled_by="rule")


class TestPromotionSkipsDiscard:
    def test_maintenance_does_not_stage_discard(self, tmp_path, pending_store) -> None:
        from bandi.memory.maintenance import _promote_to_long_term

        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodes = [
            Episode(
                episode_id="probe-1",
                summary="用户: ping → 回答: 老板，pong。收到消息，一切正常。",
                importance=0.9,
                recall_count=5,
            )
        ]
        staged = _promote_to_long_term(long_term, episodes, distiller=_discarding_distiller)
        assert staged == [], "提炼器判丢弃时不得进暂存区"
        assert pending_store.list_pending() == []
        long_term.close()

    def test_consolidation_does_not_stage_discard(self, tmp_path, pending_store) -> None:
        from bandi.memory.consolidation import consolidate_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                episode_id="todo-1",
                summary="用户: 帮我做个待办 App → 回答: 做好了老板！已生成：预览路径：canvas/todo.html",
                importance=0.9,
                recall_count=5,
            )
        )
        report = consolidate_episodes(episodic, long_term, distiller=_discarding_distiller)
        assert report.staged == [], "提炼器判丢弃时不得提名"
        assert pending_store.list_pending() == []
        episodic.close()
        long_term.close()
