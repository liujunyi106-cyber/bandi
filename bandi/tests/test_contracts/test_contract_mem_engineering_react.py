# 功能：/契约——入库价值判断 / 暂存编排 / 无人值守强制暂存
# 契约原则：钉生产主链路最终产物（long_term 行 / 暂存记录 / episodic tags），不钉实现。
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bandi.core.agent_runner import _persist_memory
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.ingest_review import review_episodic_ingest, review_ingest
from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.memory.staging import (
    MemoryPendingStore,
    get_write_origin,
    reset_pending_store,
    reset_write_origin,
    set_write_origin,
)

_VALUABLE_TEXT = (
    "结论：以后都统一用暂存方案，因此决定把后台写入改造为待批准队列，"
    "这样任何无人值守路径都不能绕过审批门。"
)


@pytest.fixture()
def pending_store(tmp_path):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


class _Manager:
    def __init__(self, long_term: LongTermMemory, episodic: EpisodicStore | None = None) -> None:
        self.long_term = long_term
        self.episodic = episodic
        self.working = None


def _ctx(
    raw_input: str = _VALUABLE_TEXT,
    final_message: str = _VALUABLE_TEXT,
    turn_index: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_results=None,
        final_message=final_message,
        current_content=None,
        raw_input=raw_input,
        turn_index=turn_index,
        terminal_reason=None,
        messages=[],
    )


class TestIngestReviewRules:
    def test_valuable_preference_accepts(self) -> None:
        verdict = review_ingest(_VALUABLE_TEXT, memory_type="preference")
        assert verdict.decision == "accept"
        assert verdict.score >= 0.7

    def test_pleasantry_thanks_single_and_emotion_reject(self) -> None:
        for text in ("你好", "谢谢啦", "嗯", "哈哈哈哈哈"):
            verdict = review_ingest(text, memory_type="business_fact")
            assert verdict.decision == "reject", text
            assert verdict.score < 0.3

    def test_plain_long_text_without_structure_rejects(self) -> None:
        # 语义变更：1 条件命中不再提请审批——历史行为 stage 让每轮普通
        # 对话都进审批中心（「不可能每次都直接提名」）。低置信内容直接丢弃，
        # 审批只保留给显式触达 / 无人值守背景 / 冲突裁决三类真触点。
        verdict = review_ingest("这是一个足够长的回复内容，" * 30, memory_type="business_fact")
        assert verdict.decision == "reject"
        assert verdict.reason.startswith("low_confidence")

    def test_explicit_remember_accepts_regardless_of_conditions(self) -> None:
        # 触点触发：用户明确说「记住」→ 直接入库（不看条件数）
        verdict = review_ingest("记住我的偏好：报告一律用中文", memory_type="business_fact")
        assert verdict.decision == "accept"
        assert "explicit_remember" in verdict.reason

    def test_two_conditions_accept(self) -> None:
        verdict = review_ingest("结论：把 config.yaml 换掉。", memory_type="business_fact")
        assert verdict.decision == "accept"

    def test_empty_candidate_rejects(self) -> None:
        assert review_ingest("", memory_type="business_fact").decision == "reject"
        assert review_ingest("   ", memory_type="business_fact").decision == "reject"


class TestPersistMemoryOrchestration:
    def test_foreground_accept_writes_with_reserved_metadata(self, tmp_path, pending_store) -> None:
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        asyncio.run(_persist_memory(_ctx(), _Manager(long_term), session_id="s-fg"))
        stored = long_term.get("turn:s-fg:1")
        assert stored is not None, "前台且判 accept 必须直写 long_term"
        assert stored.metadata["memory_type"] in {"preference", "process_conclusion"}
        assert stored.metadata["source"] == "agent_turn"
        assert pending_store.pending_count() == 0
        long_term.close()

    def test_background_origin_stages_instead_of_writing(self, tmp_path, pending_store) -> None:
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        token = set_write_origin("background")
        try:
            asyncio.run(_persist_memory(_ctx(), _Manager(long_term), session_id="s-bg"))
        finally:
            reset_write_origin(token)
        assert long_term.count() == 0, "无人值守写入不得落库（必须进暂存等批准）"
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].origin == "background"
        assert records[0].payload["target"] == "long_term"
        assert records[0].payload["conflict"] is False
        long_term.close()

    def test_conflict_stages_even_in_foreground(self, tmp_path, pending_store) -> None:
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        old_id = "turn:s-cf:1"
        long_term.store(MemoryItem(item_id=old_id, content="完全不同的旧内容版本"))
        asyncio.run(_persist_memory(_ctx(), _Manager(long_term), session_id="s-cf"))
        assert long_term.get(old_id).content == "完全不同的旧内容版本", "冲突未批准不得覆盖"
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].payload["conflict"] is True
        assert "conflict" in records[0].summary
        long_term.close()

    def test_low_value_reject_skips_write_and_stage(self, tmp_path, pending_store) -> None:
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        asyncio.run(
            _persist_memory(_ctx(raw_input="哈哈" * 30), _Manager(long_term), session_id="s-rj")
        )
        assert long_term.count() == 0
        assert pending_store.pending_count() == 0, "reject 不得产生待批准记录"
        long_term.close()

    def test_stage_failure_never_falls_back_to_direct_write(self, tmp_path) -> None:
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        reset_pending_store(MemoryPendingStore(str(blocked)))
        try:
            long_term = LongTermMemory(str(tmp_path / "lt.db"))
            token = set_write_origin("background")
            try:
                asyncio.run(_persist_memory(_ctx(), _Manager(long_term), session_id="s-fail"))
            finally:
                reset_write_origin(token)
            assert long_term.count() == 0, "暂存失败必须安全失败（不得回落直写）"
            long_term.close()
        finally:
            reset_pending_store()

    def test_episodic_tags_carry_type_and_entities(self, tmp_path, pending_store) -> None:
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        ctx = _ctx(
            raw_input='结论：把 "预算模块" 迁到新库。',
            final_message='结论：把 "预算模块" 迁到新库。',
        )
        asyncio.run(_persist_memory(ctx, _Manager(long_term, episodic), session_id="s-tags"))
        episode = episodic.get("ep:s-tags:1")
        assert episode is not None
        assert "mt:process_conclusion" in episode.tags
        assert "ent:预算模块" in episode.tags
        long_term.close()
        episodic.close()


class TestUnattendedForcedStaging:
    def test_promotion_stages_and_restores_origin(self, tmp_path, pending_store) -> None:
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                episode_id="notable",
                summary="结论：项目用 A 方案。",
                importance=0.3,
                recall_count=2,
            )
        )
        promoted = promote_notable_episodes(episodic, long_term)
        assert "notable" in promoted
        assert long_term.count() == 0, "维护晋升属无人值守路径（必须暂存）"
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].action == "promote_episode"
        assert records[0].origin == "background"
        assert records[0].payload["item"]["id"] == "consolidated:notable"
        assert get_write_origin() == "foreground", "写入后必须恢复前台来源"
        episodic.close()
        long_term.close()

    def test_consolidation_stages_and_restores_origin(self, tmp_path, pending_store) -> None:
        from bandi.memory.consolidation import consolidate_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                episode_id="high",
                summary="结论：高价值经验。",
                importance=0.8,
                recall_count=2,
            )
        )
        report = consolidate_episodes(episodic, long_term)
        assert "high" in report.staged
        assert long_term.count() == 0, "离线整合属无人值守路径（必须暂存）"
        records = pending_store.list_pending()
        assert len(records) == 1
        assert records[0].action == "consolidate_episode"
        assert get_write_origin() == "foreground"
        episodic.close()
        long_term.close()

    def test_regression_scoped_promotion_keeps_dimensions_in_payload(
        self, tmp_path, pending_store
    ) -> None:
        from bandi.memory.maintenance import promote_notable_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic.record(
            Episode(
                episode_id="scoped",
                summary="结论：项目级经验。",
                importance=0.3,
                recall_count=2,
                project_id="p1",
                employee_id="e1",
            )
        )
        promote_notable_episodes(episodic, long_term)
        record = pending_store.list_pending()[0]
        assert record.payload["project_id"] == "p1"
        assert record.payload["employee_id"] == "e1"
        episodic.close()
        long_term.close()


class TestEpisodicLayerGate:
    def test_background_origin_stages_episodic_write(self, tmp_path, pending_store) -> None:
        """契约：后台 origin 下经历层写入进暂存等批准，不得直落库。"""
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        token = set_write_origin("background")
        try:
            asyncio.run(
                _persist_memory(_ctx(), _Manager(long_term, episodic), session_id="s-bg-ep")
            )
        finally:
            reset_write_origin(token)
        assert episodic.get("ep:s-bg-ep:1") is None, "无人值守经历层写入不得直落库"
        targets = [record.payload.get("target") for record in pending_store.list_pending()]
        assert "episodic" in targets, "经历层写入必须进暂存等批准"
        assert "long_term" in targets
        long_term.close()
        episodic.close()

    def test_issue_sentence_not_killed_on_either_layer(self, tmp_path, pending_store) -> None:
        """契约（破坏者）：含问题的业务句 → 经历层记录 + 长期层落库或暂存，不得双杀。"""
        final_message = (
            "谢谢，但是数据库连接断了需要修复，请检查连接配置和重试逻辑"
            "并记住这个教训，以后先看数据库状态再答复。"
        )
        verdict = review_ingest(final_message, memory_type="business_fact")
        assert verdict.decision in {"stage", "accept"}, (
            f"含问题信号的业务句不得被寒暄短路误杀（实际 {verdict.decision}）"
        )
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        asyncio.run(
            _persist_memory(
                _ctx(raw_input=final_message),
                _Manager(long_term, episodic),
                session_id="s-issue",
            )
        )
        assert episodic.get("ep:s-issue:1") is not None, "经历层必须记录业务句"
        stored = long_term.get("turn:s-issue:1") is not None
        staged = any(
            (record.payload.get("item") or {}).get("id") == "turn:s-issue:1"
            for record in pending_store.list_pending()
        )
        assert stored or staged, "长期层不得被静默丢弃（落库或暂存至少其一）"
        assert not (stored and staged), "同一写入不得既落库又暂存"
        long_term.close()
        episodic.close()

    def test_episodic_review_only_rejects_empty_or_pleasantry(self) -> None:
        """契约：经历层判定独立于长期层——只拒空/纯寒暄。"""
        assert review_episodic_ingest("").decision == "reject"
        assert review_episodic_ingest("谢谢啦").decision == "reject"
        assert review_episodic_ingest("收到，配置已保存").decision != "reject"
        assert review_episodic_ingest("谢谢，但是数据库连接断了需要修复").decision != "reject"


class TestStagedSummaryMasking:
    def test_staged_summary_masks_secret(self, tmp_path, pending_store) -> None:
        """契约：暂存摘要生成时即脱敏——密钥原文不得落 pending 记录。"""
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        secret = "sk-live-abcdef1234567890"
        final_message = (
            f"结论：把后台任务的密钥配置改成 {secret}，以后统一用它调用接口，"
            "并且不要再把密钥写进任何日志或摘要。"
        )
        token = set_write_origin("background")
        try:
            asyncio.run(
                _persist_memory(
                    _ctx(raw_input=final_message), _Manager(long_term), session_id="s-secret"
                )
            )
        finally:
            reset_write_origin(token)
        records = pending_store.list_pending()
        assert records, "后台长句必须进暂存"
        for record in records:
            assert secret not in record.summary, f"暂存摘要不得泄漏密钥：{record.summary}"
        long_term.close()


class TestEnricherSetterLifecycle:
    @pytest.mark.asyncio
    async def test_second_turn_without_entity_store_clears_enricher(
        self, tmp_path, monkeypatch
    ) -> None:
        """契约：复用 manager 时第二轮 entity_store=None 必须清除旧图增强。"""
        from unittest.mock import patch

        from bandi.core.agent_runner import run_agent_turn
        from bandi.core.config import BandiConfig
        from bandi.core.message import LLMResponse
        from bandi.llm.mock import MockProvider
        from bandi.memory.session.store import SessionStore
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.triple_store import OntologyStore

        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        store = SessionStore(str(tmp_path / "sess.db"))
        sid = store.create_session(mode="execute")
        entity_store = OntologyStore(open_ontology(str(tmp_path / "ontology.db")))
        provider = MockProvider()
        provider.add_response(LLMResponse(content="第一轮完成"))
        provider.add_response(LLMResponse(content="第二轮完成"))
        cfg = BandiConfig()
        cfg.llm.api_key = ""
        cfg.agent.max_iterations = 1
        cfg.prefetch.enabled = False

        class _MemoryStub:
            def __init__(self) -> None:
                self.enricher: Any = None

            def search_unified(self, query: str, **kwargs: Any) -> list[Any]:
                return []

            def close(self) -> None:
                pass

        memory = _MemoryStub()
        with patch("bandi.core.agent_runner.build_provider", return_value=provider):
            await run_agent_turn(
                user_input="第一问",
                session_id=sid,
                session_store=store,
                triple_ontology_store=entity_store,
                memory_manager=memory,
                bandi_config=cfg,
            )
            assert memory.enricher is not None, "有实体源时必须回填只读增强"
            await run_agent_turn(
                user_input="第二问",
                session_id=sid,
                session_store=store,
                triple_ontology_store=None,
                memory_manager=memory,
                bandi_config=cfg,
            )
            assert memory.enricher is None, "第二轮无实体源必须清除上一轮遗留增强"
        store.close()
        entity_store.close()


# ── 审批预览提炼（memory_preview）─────────────────────────────────────────


class TestMemoryPreview:
    """契约：待审批记忆预览是**提炼主体**，不是回复开头的寒暄/自我介绍。"""

    def test_greeting_and_self_intro_skipped(self) -> None:
        from bandi.core.agent_runner import memory_preview

        content = "你好，老板！我是 Bandi 的助手。\n竞品价格区间为 100-200 元。"
        preview = memory_preview(content, "business_fact")
        assert preview.startswith("事实 · ")
        assert "竞品价格区间" in preview
        assert "Bandi 的助手" not in preview

    def test_type_label_humanized(self) -> None:
        from bandi.core.agent_runner import memory_preview

        assert memory_preview("用户偏好中文报告", "preference").startswith("偏好 · ")
        assert memory_preview("先查订单再退款", "procedural").startswith("步骤 · ")

    def test_plain_content_unaffected(self) -> None:
        from bandi.core.agent_runner import memory_preview

        preview = memory_preview("客户退款需财务复核", "business_fact")
        assert preview == "事实 · 客户退款需财务复核"

    def test_long_body_truncated(self) -> None:
        from bandi.core.agent_runner import memory_preview

        preview = memory_preview("长内容" * 100, "business_fact", max_chars=40)
        assert len(preview) <= len("事实 · ") + 40

    def test_all_greeting_falls_back_to_original(self) -> None:
        # 破坏者：整段都是寒暄（无正文）→ 回落原文，不产出空预览
        from bandi.core.agent_runner import memory_preview

        preview = memory_preview("你好！", "business_fact")
        assert preview == "事实 · 你好！"
