"""功能：评审裁决桥契约——输出解析 / 隐藏评审卡构建与去重 / 契约解析失败提示渲染。

钉行为不钉实现：裁决 JSON 取最后一个可解析对象、行内前后缀容错；
评审卡防重复建卡（终态后可重建）；自评跳过；隐藏卡不上看板。
"""

from __future__ import annotations

from bandi.company.phase import Phase
from bandi.company.review_bridge import (
    build_review_card,
    find_live_review_card,
    parse_review_verdict,
)
from bandi.company.store import WorkItemStore
from bandi.company.work_item import (
    WorkItem,
    WorkItemTurnType,
    create_work_item,
    is_hidden_work_item,
)


class TestParseReviewVerdict:
    def test_approve_in_prose(self) -> None:
        output = '评审完成。\n{"review_verdict":"approve","summary":"满足验收标准"}\n'
        verdict = parse_review_verdict(output)
        assert verdict is not None
        assert verdict.verdict == "approve"
        assert verdict.summary == "满足验收标准"

    def test_reject_with_issues(self) -> None:
        output = (
            '{"review_verdict":"reject","summary":"产物缺失",'
            '"blocking_issues":["补 market_brief.md","加入预算约束"],'
            '"followups":["补充竞品对比"]}'
        )
        verdict = parse_review_verdict(output)
        assert verdict is not None
        assert verdict.verdict == "reject"
        assert verdict.blocking_issues == ["补 market_brief.md", "加入预算约束"]
        assert verdict.followups == ["补充竞品对比"]

    def test_last_object_wins(self) -> None:
        output = (
            '{"review_verdict":"reject","summary":"初判"}\n'
            "重新检查后：\n"
            '{"review_verdict":"approve","summary":"复核通过"}'
        )
        verdict = parse_review_verdict(output)
        assert verdict is not None and verdict.verdict == "approve"

    def test_prefix_suffix_tolerated(self) -> None:
        output = '结论: {"review_verdict":"approve","summary":"ok"} 完毕'
        verdict = parse_review_verdict(output)
        assert verdict is not None and verdict.verdict == "approve"

    def test_missing_or_invalid_returns_none(self) -> None:
        assert parse_review_verdict("") is None
        assert parse_review_verdict("没有裁决") is None
        assert parse_review_verdict('{"review_verdict":"maybe"}') is None
        assert parse_review_verdict('{"summary":"无裁决字段"}') is None


def _child(store: WorkItemStore, *, role: str = "worker") -> WorkItem:
    child = create_work_item(
        title="市场调研",
        turn_type=WorkItemTurnType.EXECUTE,
        description="调研无线耳机市场",
        assignee_role=role,
        metadata={},
        phase=Phase.AWAITING_MANAGER_REVIEW,
    )
    child.runtime_metadata["_last_output"] = "交付报告" * 1000
    store.save(child)
    return child


class TestBuildReviewCard:
    def test_creates_hidden_card_with_metadata(self) -> None:
        store = WorkItemStore(":memory:")
        child = _child(store)
        card = build_review_card(child, "lead", store)
        assert card is not None
        assert card.assignee_role == "lead"
        assert card.turn_type == WorkItemTurnType.REVIEW
        assert is_hidden_work_item(card) is True
        assert card.metadata["review_target_work_item_id"] == child.id
        assert len(card.metadata["review_completion_report"]) <= 2000

    def test_dedupe_while_live_and_recreate_after_terminal(self) -> None:
        store = WorkItemStore(":memory:")
        child = _child(store)
        first = build_review_card(child, "lead", store)
        assert first is not None
        assert build_review_card(child, "lead", store) is None
        assert find_live_review_card(store, child.id) is not None
        first.phase = Phase.APPROVED
        store.save(first)
        assert find_live_review_card(store, child.id) is None
        assert build_review_card(child, "lead", store) is not None

    def test_self_review_and_empty_reviewer_skipped(self) -> None:
        store = WorkItemStore(":memory:")
        child = _child(store, role="lead")
        assert build_review_card(child, "lead", store) is None
        assert build_review_card(child, "", store) is None

    def test_review_card_copies_target_contract(self) -> None:
        store = WorkItemStore(":memory:")
        child = _child(store)
        child.metadata["prompt_contract"] = {
            "version": 2,
            "task_brief": "调研无线耳机市场",
            "assignment_context": {
                "upstream_intent_summary": "用户要进入耳机市场",
                "deliverables": ["market_brief.md"],
                "acceptance_criteria": ["包含 3 个竞品"],
            },
        }
        store.save(child)
        card = build_review_card(child, "lead", store)
        assert card is not None
        assert card.metadata["review_target_prompt_contract"] == child.metadata["prompt_contract"]
        assert card.metadata["review_target_acceptance_criteria"] == ["包含 3 个竞品"]
        assert card.metadata["review_target_description"] == "调研无线耳机市场"


class TestParseFailureHintRender:
    def _work_item(self, *, hint: bool) -> dict:
        metadata = {
            "runtime_model": "multi_team_org",
            "review_execution_work_item": True,
            "review_target_work_item_id": "wi-target",
        }
        if hint:
            metadata["review_parse_failure_hint"] = True
        return {
            "id": "wi-review",
            "title": "评审卡",
            "turn_type": "execute",
            "metadata": metadata,
            "runtime_metadata": {},
        }

    def test_hint_rendered_when_flag_set(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        text = build_company_work_item_contract(self._work_item(hint=True))
        assert "Parse Failure Hint" in text

    def test_hint_absent_without_flag(self) -> None:
        from bandi.company.company_runtime_contract import build_company_work_item_contract

        text = build_company_work_item_contract(self._work_item(hint=False))
        assert "Parse Failure Hint" not in text
