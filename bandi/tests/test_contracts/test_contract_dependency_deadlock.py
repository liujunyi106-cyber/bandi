# 功能：契约——久置 needs_attention 依赖不得让下游永久等待，旧模板残留任务图须被容忍。
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bandi.company.phase import Phase
from bandi.company.team_template import TeamTemplate
from bandi.company.work_item import WorkItemTurnType
from bandi.company.work_item_transition import (
    ABANDONED_ATTENTION_MINUTES,
    compute_abandoned_work_item_ids,
)


def _template(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "template_id": "demo-team",
        "name": "示例团队",
        "roles": [
            {"id": "lead", "name": "负责人"},
            {"id": "designer", "name": "设计"},
        ],
    }
    base.update(overrides)
    return base


class TestLegacyWorkItemGraphTolerance:
    """旧模板文件里的任务图不应让整份模板被判非法。"""

    def test_legacy_work_item_graph_is_dropped(self) -> None:
        template = TeamTemplate(
            **_template(
                work_item_templates=[
                    {"id": "a", "title": "A", "role_id": "lead"},
                    {
                        "id": "b",
                        "title": "B",
                        "role_id": "designer",
                        "dependencies": ["a"],
                        "gate": {"type": "review", "reviewer_role": "designer"},
                    },
                ]
            )
        )
        assert template.template_id == "demo-team"
        assert [r.id for r in template.roles] == ["lead", "designer"]

    def test_model_exposes_no_work_item_graph(self) -> None:
        """模板仍只描述组织：模型不得暴露任务图字段。"""
        template = TeamTemplate(**_template(work_item_templates=[{"id": "x"}]))
        dumped = template.model_dump()
        assert "work_item_templates" not in dumped

    def test_template_without_graph_still_loads(self) -> None:
        template = TeamTemplate(**_template())
        assert template.name == "示例团队"

    def test_unknown_field_still_rejected(self) -> None:
        """仅容忍已知遗留字段，其它未知字段必须继续 fail-closed。"""
        import pytest

        with pytest.raises(ValueError, match="Extra inputs"):
            TeamTemplate(**_template(unexpected_field=1))


class TestAbandonedDependencies:
    """久置 needs_attention 依赖算作放弃，让下游能结算。"""

    def test_old_needs_attention_is_abandoned(self) -> None:
        now = datetime.now(UTC)
        old = _item(
            "blocker",
            Phase.NEEDS_ATTENTION,
            updated_at=now - timedelta(minutes=ABANDONED_ATTENTION_MINUTES + 5),
        )
        fresh = _item(
            "fresh",
            Phase.NEEDS_ATTENTION,
            updated_at=now - timedelta(minutes=5),
        )
        assert compute_abandoned_work_item_ids([old, fresh]) == {"blocker"}

    def test_other_phases_never_abandoned(self) -> None:
        now = datetime.now(UTC)
        stale = now - timedelta(minutes=1000)
        items = [
            _item("a", Phase.RUNNING, updated_at=stale),
            _item("b", Phase.APPROVED, updated_at=stale),
            _item("c", Phase.WAITING_DEPENDENCIES, updated_at=stale),
        ]
        assert compute_abandoned_work_item_ids(items) == set()

    def test_missing_timestamp_is_not_abandoned(self) -> None:
        """无法证明久置的卡不得被误判为放弃。"""
        item = _item("no-ts", Phase.NEEDS_ATTENTION, updated_at=None)
        assert compute_abandoned_work_item_ids([item]) == set()

    def test_downstream_of_abandoned_dependency_settles(self, tmp_path: Path) -> None:
        """核心契约：阻塞者久置放弃后，下游不再无限 waiting_dependencies。"""
        from bandi.company.store import WorkItemStore
        from bandi.company.work_item import create_work_item
        from bandi.company.work_item_transition import refresh_dependents

        store = WorkItemStore(str(tmp_path / "company.db"))
        blocker = create_work_item(
            title="阻塞者",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="researcher",
        )
        blocker.phase = Phase.NEEDS_ATTENTION
        blocker.updated_at = datetime.now(UTC) - timedelta(minutes=ABANDONED_ATTENTION_MINUTES + 30)
        store.save(blocker)

        waiter = create_work_item(
            title="下游",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="analyst",
        )
        waiter.phase = Phase.WAITING_DEPENDENCIES
        waiter.metadata["dependency_work_item_ids"] = [blocker.id]
        store.save(waiter)

        refresh_dependents(store)

        refreshed = store.get(waiter.id)
        assert refreshed is not None
        assert refreshed.phase != Phase.WAITING_DEPENDENCIES, (
            f"阻塞者已久置放弃，下游不得停在等待态（实际 {refreshed.phase}）"
        )
        settlement = dict((refreshed.metadata or {}).get("dependency_settlement") or {})
        released = list(settlement.get("failed") or []) + list(settlement.get("stuck") or [])
        assert blocker.id in released, "结算记录必须写明被放弃的阻塞者"
        store.close()

    def test_fresh_needs_attention_still_blocks(self, tmp_path: Path) -> None:
        """刚进 needs_attention 的卡仍应等待人工，不得被提前放行。"""
        from bandi.company.store import WorkItemStore
        from bandi.company.work_item import create_work_item
        from bandi.company.work_item_transition import refresh_dependents

        store = WorkItemStore(str(tmp_path / "company.db"))
        blocker = create_work_item(
            title="阻塞者",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="researcher",
        )
        blocker.phase = Phase.NEEDS_ATTENTION
        blocker.updated_at = datetime.now(UTC) - timedelta(minutes=1)
        store.save(blocker)

        waiter = create_work_item(
            title="下游",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="analyst",
        )
        waiter.phase = Phase.WAITING_DEPENDENCIES
        waiter.metadata["dependency_work_item_ids"] = [blocker.id]
        store.save(waiter)

        refresh_dependents(store)

        refreshed = store.get(waiter.id)
        assert refreshed is not None
        assert refreshed.phase == Phase.WAITING_DEPENDENCIES, (
            "新近的 needs_attention 仍属待人工，下游必须继续等"
        )
        store.close()

    def test_real_failure_still_blocks_ordinary_card(self, tmp_path: Path) -> None:
        """前置真实失败时，普通卡仍停在等待态（分诊由人工决定）。"""
        from bandi.company.store import WorkItemStore
        from bandi.company.work_item import create_work_item
        from bandi.company.work_item_transition import refresh_dependents

        store = WorkItemStore(str(tmp_path / "company.db"))
        child = create_work_item(
            title="失败的前置",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="researcher",
        )
        child.phase = Phase.FAILED
        store.save(child)

        parent = create_work_item(
            title="下游",
            turn_type=WorkItemTurnType.EXECUTE,
            assignee_role="analyst",
        )
        parent.phase = Phase.WAITING_DEPENDENCIES
        parent.metadata["dependency_work_item_ids"] = [child.id]
        store.save(parent)

        refresh_dependents(store)

        refreshed = store.get(parent.id)
        assert refreshed is not None
        assert refreshed.phase == Phase.WAITING_DEPENDENCIES, (
            "真实失败靠人工分诊，普通卡不得自动放行"
        )
        store.close()


def _item(
    item_id: str,
    phase: Phase,
    *,
    updated_at: datetime | None,
) -> Any:
    """功能：构造只带判定所需字段的工作项。"""

    class _Item:
        pass

    item = _Item()
    item.id = item_id
    item.phase = phase
    item.updated_at = updated_at
    item.metadata = {}
    return item
