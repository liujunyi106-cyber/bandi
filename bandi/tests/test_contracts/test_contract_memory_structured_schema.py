# 功能：契约——记忆结构化 schema（B2）：直写补 structured、草稿提名结构化、无原文包装
#
# 行为不变式（钉行为，不钉实现）：
#   1. build_structured 偏好类 → {kind:"preference", key:<规范属性名>, value:...}
#   2. build_structured 三元组形态 predicate 必在 tbox.RELATION_TERMS 白名单内
#   3. 抽不出（无谓词无属性词）→ None——直写侧不阻塞写入
#   4. structured.key 恒为规范化 snake_case
#   5. 草稿提名：item.content 与 pending summary 不含「用户: … → 回答:」包装；
#      evidence 留原句；structured 三件套（kind/key）在 payload 可读回
#   6. 提炼失败（distiller 抛异常）→ 降级仍产出 kind/key（提名不丢）
#   7. 直写生产主链路：_persist_memory 写入条目带 structured

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bandi.core.agent_runner import _persist_memory
from bandi.memory.distill import rule_distill
from bandi.memory.episodic import Episode, EpisodicStore
from bandi.memory.long_term import LongTermMemory
from bandi.memory.staging import MemoryPendingStore, reset_pending_store
from bandi.memory.structured import build_structured, normalize_key
from bandi.memory.tbox import RELATION_TERMS


class TestNormalizeKey:
    def test_english_normalization(self) -> None:
        assert normalize_key("Export Format") == "export_format"
        assert normalize_key("  Report--Format  ") == "report_format"

    def test_too_long_truncated(self) -> None:
        assert len(normalize_key("a" * 80)) <= 40

    def test_non_ascii_invalid_returns_empty(self) -> None:
        assert normalize_key("导出格式") == ""
        assert normalize_key("") == ""


class TestBuildStructuredRule:
    def test_preference_with_attribute_key(self) -> None:
        data = build_structured("我偏好把月度报表导出成 CSV 格式")
        assert data is not None
        assert data["kind"] == "preference"
        assert data["key"] == "format"
        assert "CSV" in data["value"]

    def test_preference_without_attribute_falls_back_to_predicate_key(self) -> None:
        data = build_structured("我特别喜欢数字 88 和靛蓝色，记住啦。")
        assert data is not None
        assert data["kind"] == "preference"
        assert data["key"] == "preference"
        assert data["value"]

    def test_fact_triple_predicate_in_whitelist(self) -> None:
        data = build_structured("项目 Alpha 属于产品线 Beta")
        assert data is not None
        assert data["kind"] == "fact"
        if "predicate" in data:
            assert data["predicate"] in RELATION_TERMS, "谓词必须落在白名单内"

    def test_fact_key_value_without_predicate(self) -> None:
        data = build_structured("服务器端口是 7421")
        assert data is not None
        assert data["kind"] == "fact"
        assert data["key"] == "port"
        assert "7421" in data["value"]

    def test_unable_to_extract_returns_none(self) -> None:
        assert build_structured("现在几点了？") is None
        assert build_structured("") is None

    def test_attribute_coverage_broad(self) -> None:
        """属性词表覆盖交付格式/财务/联系/时间/技术栈等主要属性面（弱 key 兜底率下降）。"""
        cases = {
            "我们项目的通知渠道走企业微信，默认同步到群": "notification",
            "数据库统一用 PostgreSQL 16 版本": "database",
            "上线时间定在周三上午": "release",
            "并发上限统一 200，超时 30 秒": "concurrency",
            "审批流程必须走两级评审": "approval",
            "我忌口香菜，点餐都帮我备注": "dietary",
        }
        for text, expected_key in cases.items():
            data = build_structured(text)
            assert data is not None, text
            assert data["key"] == expected_key, f"{text!r} 期望 key={expected_key}，实际 {data}"

    def test_weak_key_marked_for_merge_guard(self) -> None:
        """弱 key 判定：属性词未命中的兜底键须被标记——B3 去重据此退回内容指纹。"""
        from bandi.memory.structured import is_weak_key

        fallback = build_structured("我特别喜欢数字 88 和靛蓝色，记住啦。")
        assert fallback is not None
        assert is_weak_key(fallback["key"]) is True, "兜底键须标为弱 key"

        concrete = build_structured("我偏好把月度报表导出成 CSV 格式")
        assert concrete is not None
        assert is_weak_key(concrete["key"]) is False, "命中属性词的键不是弱键"


class _Manager:
    def __init__(self, long_term: LongTermMemory, episodic: EpisodicStore | None = None) -> None:
        self.long_term = long_term
        self.episodic = episodic
        self.working = None


def _ctx(
    raw_input: str = "",
    final_message: str = "好的。",
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


class TestDirectWriteStructured:
    def test_direct_write_attaches_structured(self, tmp_path) -> None:
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        asyncio.run(
            _persist_memory(
                _ctx(raw_input="记住：我以后偏好把月度报表导出成 CSV 格式，预算口径统一用人民币。"),
                _Manager(long_term),
                session_id="s-b2",
            )
        )
        item = long_term.get("turn:s-b2:1")
        assert item is not None, "前置：显式记住必须直写"
        structured = (item.metadata or {}).get("structured")
        assert isinstance(structured, dict), "直写条目必须带 structured"
        assert structured["kind"] == "preference"
        assert structured["key"] == "format"
        assert item.metadata.get("evidence"), "原句必须留底 evidence"
        long_term.close()

    def test_direct_write_without_rule_extraction_still_persists(self, tmp_path) -> None:
        # 抽不出结构化不得阻塞写入（仅 content，无 structured 字段）
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        asyncio.run(
            _persist_memory(
                _ctx(raw_input="记住：暗号是 pineapple-77 呀。"),
                _Manager(long_term),
                session_id="s-b2n",
            )
        )
        item = long_term.get("turn:s-b2n:1")
        assert item is not None, "抽不出不得阻塞写入"
        long_term.close()


@pytest.fixture()
def pending_store(tmp_path):
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _seed_high_value(episodic: EpisodicStore, summary: str) -> None:
    episodic.record(Episode(episode_id="ep-b2", summary=summary, importance=0.85, recall_count=2))


class TestNominationStructured:
    def test_nomination_content_and_summary_have_no_wrapper(self, tmp_path, pending_store) -> None:
        from bandi.memory.consolidation import consolidate_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_high_value(
            episodic,
            "用户: 我们项目的部署端口是 7421，上线时间定在周三。 → 回答: 收到，已记录端口 7421。",
        )
        report = consolidate_episodes(
            episodic,
            long_term,
            distiller=lambda ep: rule_distill(ep.summary, importance=ep.importance),
        )
        assert report.staged == ["ep-b2"], "前置：高价值情节必须提名"

        records = pending_store.list_pending()
        assert len(records) == 1
        record = records[0]
        item = record.payload["item"]
        assert "→ 回答:" not in item["content"], "提名 content 不得含对话包装"
        assert "用户:" not in record.summary, "消息中心摘要不得含对话包装"
        structured = (item.get("metadata") or {}).get("structured")
        assert isinstance(structured, dict)
        assert structured.get("kind"), "structured.kind 必存在"
        assert structured.get("key"), "structured.key 必存在"
        assert "→ 回答:" in (item.get("metadata") or {}).get("evidence", ""), "原句留底"
        episodic.close()
        long_term.close()

    def test_nomination_survives_distiller_failure(self, tmp_path, pending_store) -> None:
        from bandi.memory.consolidation import consolidate_episodes

        episodic = EpisodicStore(str(tmp_path / "ep.db"))
        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        _seed_high_value(episodic, "用户: 记住偏好。 → 回答: 好的。")

        def _boom(_episode: Episode) -> None:
            raise RuntimeError("llm offline")

        report = consolidate_episodes(episodic, long_term, distiller=_boom)
        assert report.staged == ["ep-b2"], "提炼失败不得丢失提名"
        item = pending_store.list_pending()[0].payload["item"]
        structured = (item.get("metadata") or {}).get("structured")
        assert isinstance(structured, dict)
        assert structured.get("kind") and structured.get("key"), "降级仍产出 kind/key"
        episodic.close()
        long_term.close()
