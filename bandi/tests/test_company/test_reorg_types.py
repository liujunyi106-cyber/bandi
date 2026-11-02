from __future__ import annotations

from bandi.company.reorg_types import (
    ReorgEventKind,
    ReorgRiskLevel,
    ReorgScope,
    classify_risk,
    infer_scope,
    normalize_changeset,
)


class TestReorgEventKind:
    def test_from_dict_unknown_falls_back(self) -> None:
        """ReorgEventRecord.from_dict 对未知 event_kind 回退 PROPOSED。"""
        from bandi.company.reorg_types import ReorgEventRecord

        rec = ReorgEventRecord.from_dict({"event_kind": "nonexistent", "summary": "x"})
        assert rec.event_kind == ReorgEventKind.PROPOSED

    def test_known_kind_preserved(self) -> None:
        from bandi.company.reorg_types import ReorgEventRecord

        rec = ReorgEventRecord.from_dict({"event_kind": "approved"})
        assert rec.event_kind == ReorgEventKind.APPROVED


class TestInferScope:
    def test_role_changes_becomes_org_mutation(self) -> None:
        from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange

        cs = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="add", role_id="r1")], task_adjustments=[]
        )
        assert infer_scope(cs) == ReorgScope.ORG_MUTATION

    def test_no_role_changes_becomes_task_adjustment(self) -> None:
        from bandi.company.reorg_types import ReorgChangeSet

        cs = ReorgChangeSet(role_changes=[], task_adjustments=[])
        assert infer_scope(cs) == ReorgScope.TASK_ADJUSTMENT


class TestClassifyRisk:
    def test_org_mutation_is_high(self) -> None:
        from bandi.company.reorg_types import ReorgChangeSet, ReorgRoleChange

        cs = ReorgChangeSet(
            role_changes=[ReorgRoleChange(action="add", role_id="r1")], task_adjustments=[]
        )
        assert classify_risk(infer_scope(cs), cs) == ReorgRiskLevel.HIGH

    def test_valid_task_actions_are_low(self) -> None:
        from bandi.company.reorg_types import ReorgChangeSet, ReorgTaskAdjustment

        adj = ReorgTaskAdjustment(task_id="t1", action="reassign", new_role_id="r2")
        cs = ReorgChangeSet(role_changes=[], task_adjustments=[adj])
        assert classify_risk(ReorgScope.TASK_ADJUSTMENT, cs) == ReorgRiskLevel.LOW

    def test_invalid_task_action_is_medium(self) -> None:
        from bandi.company.reorg_types import ReorgChangeSet, ReorgTaskAdjustment

        adj = ReorgTaskAdjustment(task_id="t1", action="nuclear_launch")
        cs = ReorgChangeSet(role_changes=[], task_adjustments=[adj])
        assert classify_risk(ReorgScope.TASK_ADJUSTMENT, cs) == ReorgRiskLevel.MEDIUM


class TestNormalizeChangeset:
    def test_dict_elements_converted(self) -> None:
        from bandi.company.reorg_types import ReorgChangeSet

        raw = ReorgChangeSet.from_dict(
            {
                "role_changes": [
                    {"action": "add", "role_id": "r1", "role": {"id": "r1", "name": "R"}}
                ],
                "task_adjustments": [{"task_id": "t1", "action": "reassign"}],
            }
        )
        normalized = normalize_changeset(raw)
        assert isinstance(normalized.role_changes[0], type(raw.role_changes[0]))
        assert normalized.role_changes[0].role_id == "r1"
