# 功能：契约 长期记忆的三档出口——显式直写 / 有价值进草稿池 / 无价值丢弃
#
# 行为不变式：
#   ① 只有用户显式「记住」才允许跳过草稿直写落库；
#   ② 其余通过价值审查的候选先进草稿池（gate=draft），不进审批队列；
#   ③ 价值审查不过的候选既不落库也不进池（直接丢弃）；
#   ④ 草稿池超容量后触发再过滤：低于最低分的丢弃，剩下的按价值分取前 N 条晋级审批；
#   ⑤ 未超容量时零打扰——审批接口只列已晋级的记录，草稿池只回条数；
#   ⑥ 老记录（没有 gate 字段）按审批队列处理，历史待批不会凭空消失。

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from bandi.memory.decide import decide_long_term
from bandi.memory.long_term import MemoryItem
from bandi.memory.staging import (
    GATE_DRAFT,
    GATE_REVIEW,
    MemoryPendingStore,
    reset_pending_store,
    stage_long_term_write,
)


@pytest.fixture()
def pending_store(tmp_path: Path) -> MemoryPendingStore:
    store = MemoryPendingStore(str(tmp_path / "memory"))
    reset_pending_store(store)
    yield store
    reset_pending_store()


def _item(content: str, item_id: str = "turn:1") -> MemoryItem:
    return MemoryItem(item_id=item_id, content=content, importance=0.6, metadata={})


class TestDecisionRoutes:
    def test_explicit_remember_is_direct_write(self) -> None:
        decision = decide_long_term(raw_input="记住：部署端口是 8080", final_message="好的")
        assert decision.write is True
        assert decision.explicit is True, "显式记住必须标记 explicit，否则会被误送草稿"

    def test_user_statement_is_not_explicit(self) -> None:
        """用户陈述事实不等于显式要求记住——必须走草稿池等批。"""
        decision = decide_long_term(raw_input="我的习惯是先写测试再改代码", final_message="明白")
        assert decision.write is True
        assert decision.explicit is False
        assert decision.value_score > 0

    def test_plain_request_is_dropped(self) -> None:
        decision = decide_long_term(
            raw_input="帮我整理蓝鲸栖息地资料", final_message="结论：蓝鲸栖息于全球海洋"
        )
        assert decision.write is False, "请求型输入不是可记事实，不得落库也不得进池"

    def test_execution_report_is_dropped(self) -> None:
        decision = decide_long_term(
            raw_input="待办 App 已经做好了，预览路径在 out/todo.html",
            final_message="收到",
        )
        assert decision.write is False


class TestDraftPool:
    def test_staged_write_lands_in_draft_not_review(
        self, pending_store: MemoryPendingStore
    ) -> None:
        stage_long_term_write(
            _item("偏好：先写测试再改代码"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="偏好 · 先写测试",
            origin="foreground",
            gate=GATE_DRAFT,
            value_score=0.6,
        )
        assert len(pending_store.list_pending(GATE_DRAFT)) == 1
        assert pending_store.list_pending(GATE_REVIEW) == [], "草稿池条目不得直接出现在审批队列"

    def test_conflict_always_goes_to_review(self, pending_store: MemoryPendingStore) -> None:
        stage_long_term_write(
            _item("冲突内容"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="冲突",
            origin="foreground",
            conflict=True,
            gate=GATE_REVIEW,
        )
        assert len(pending_store.list_pending(GATE_REVIEW)) == 1

    def test_promotion_picks_top_scores(self, pending_store: MemoryPendingStore) -> None:
        for index, score in enumerate((0.45, 0.9, 0.6, 0.75, 0.5)):
            stage_long_term_write(
                _item(f"候选 {index}", item_id=f"turn:{index}"),
                project_id=None,
                employee_id=None,
                action="store_long_term",
                summary=f"候选 {index}",
                origin="foreground",
                gate=GATE_DRAFT,
                value_score=score,
            )
        promoted = pending_store.promote_drafts(pool_limit=3, batch_limit=2, min_score=0.4)
        assert promoted == 2, f"超容量后只应晋级前 2 条，实际 {promoted}"
        review = pending_store.list_pending(GATE_REVIEW)
        assert {record.value_score for record in review} == {0.9, 0.75}

    def test_below_min_score_is_discarded(self, pending_store: MemoryPendingStore) -> None:
        for index, score in enumerate((0.1, 0.9, 0.2, 0.95)):
            stage_long_term_write(
                _item(f"候选 {index}", item_id=f"turn:{index}"),
                project_id=None,
                employee_id=None,
                action="store_long_term",
                summary=f"候选 {index}",
                origin="foreground",
                gate=GATE_DRAFT,
                value_score=score,
            )
        pending_store.promote_drafts(pool_limit=2, batch_limit=1, min_score=0.4)
        remaining = sorted(record.value_score for record in pending_store.list_pending())
        assert remaining == [0.9, 0.95], f"低于最低分的草稿必须被丢弃，实际留存 {remaining}"
        assert [record.value_score for record in pending_store.list_pending(GATE_REVIEW)] == [
            0.95
        ], "晋级只取最高分那一条"

    def _stage_aged(
        self, store: MemoryPendingStore, item_id: str, score: float, hours: float
    ) -> None:
        """功能：直接落一条「若干小时前产生」的草稿（时间兜底用例需要老记录）。"""
        import time as _time

        store.stage(
            {
                "action": "store_long_term",
                "summary": item_id,
                "origin": "foreground",
                "gate": GATE_DRAFT,
                "value_score": score,
                "created_at": _time.time() - hours * 3600,
                "payload": {
                    "target": "long_term",
                    "item": {"item_id": item_id, "content": item_id},
                },
            }
        )

    def test_time_fallback_promotes_one_when_starved(
        self, pending_store: MemoryPendingStore
    ) -> None:
        """时间兜底：草稿没超容量但等太久，也必须晋级一条，否则记忆永远不落库。"""
        self._stage_aged(pending_store, "turn:old-a", 0.5, 25)
        self._stage_aged(pending_store, "turn:old-b", 0.8, 25)
        promoted = pending_store.promote_drafts(
            pool_limit=30, batch_limit=5, min_score=0.4, force_after_hours=24
        )
        assert promoted == 1, "时间兜底一次只晋级最高分那一条"
        review = pending_store.list_pending(GATE_REVIEW)
        assert [record.value_score for record in review] == [0.8]

    def test_time_fallback_disabled_when_zero(self, pending_store: MemoryPendingStore) -> None:
        self._stage_aged(pending_store, "turn:old-c", 0.9, 100)
        assert (
            pending_store.promote_drafts(
                pool_limit=30, batch_limit=5, min_score=0.4, force_after_hours=0
            )
            == 0
        )

    def test_not_over_limit_is_noop(self, pending_store: MemoryPendingStore) -> None:
        stage_long_term_write(
            _item("唯一候选"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="唯一候选",
            origin="foreground",
            gate=GATE_DRAFT,
            value_score=0.9,
        )
        assert pending_store.promote_drafts(pool_limit=30, batch_limit=5, min_score=0.4) == 0
        assert pending_store.list_pending(GATE_REVIEW) == [], "未超容量不得打扰用户"


class TestLegacyCompatibility:
    def test_record_without_gate_defaults_to_review(self, tmp_path: Path) -> None:
        """破坏者视角：历史待批记录没有 gate 字段，不能被当成草稿藏起来。"""
        store = MemoryPendingStore(str(tmp_path / "legacy"))
        reset_pending_store(store)
        target = tmp_path / "legacy" / "pending" / "memory"
        target.mkdir(parents=True, exist_ok=True)
        (target / "abcd1234.json").write_text(
            json.dumps(
                {
                    "id8": "abcd1234",
                    "subsystem": "memory",
                    "action": "store_long_term",
                    "summary": "历史待批",
                    "origin": "background",
                    "created_at": 1.0,
                    "payload": {"target": "long_term"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            assert len(store.list_pending(GATE_REVIEW)) == 1
            assert store.list_pending(GATE_DRAFT) == []
        finally:
            reset_pending_store()


class TestPendingApiGate:
    def test_api_lists_review_only_and_reports_draft_count(
        self, pending_store: MemoryPendingStore, tmp_path: Path
    ) -> None:
        stage_long_term_write(
            _item("进审批的", item_id="turn:a"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="进审批的",
            origin="background",
            gate=GATE_REVIEW,
            value_score=0.9,
        )
        stage_long_term_write(
            _item("还在池里的", item_id="turn:b"),
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="还在池里的",
            origin="foreground",
            gate=GATE_DRAFT,
            value_score=0.5,
        )
        app_mod = importlib.import_module("bandi.api.app")
        payload: dict[str, Any] = asyncio.run(app_mod.list_memory_pending())
        assert len(payload["pending"]) == 1
        assert payload["pending"][0]["summary"] == "进审批的"
        assert payload["drafts"] == 1, "草稿池只回条数，不进审批列表"

    def test_conflict_row_exposes_diff_and_approve_returns_receipt(
        self, pending_store: MemoryPendingStore, tmp_path: Path, monkeypatch
    ) -> None:
        """冲突裁决必须能看清旧值与新值；批准后要回执写入了哪条。"""
        from bandi.memory.long_term import LongTermMemory

        long_term = LongTermMemory(str(tmp_path / "lt.db"))
        long_term.store(MemoryItem(item_id="turn:d", content="旧值：端口 7421"))
        item = MemoryItem(item_id="turn:d", content="新值：端口 8080", metadata={})
        stage_long_term_write(
            item,
            project_id=None,
            employee_id=None,
            action="store_long_term",
            summary="端口变更",
            origin="foreground",
            conflict=True,
            gate=GATE_REVIEW,
        )
        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setattr(app_mod, "_memory_replay_overrides", {"long_term": long_term})
        payload: dict[str, Any] = asyncio.run(app_mod.list_memory_pending())
        row = payload["pending"][0]
        assert "8080" in row["diff"]["new"]
        assert "7421" in row["diff"]["old"], "冲突卡必须能对照旧值，否则只能盲批"

        record_id = row["id"]
        result = asyncio.run(app_mod.approve_memory_pending(record_id))
        assert result["approved"] is True
        assert result["target_id"] == "turn:d", "批准必须回执写入了哪条记忆"
        long_term.close()
