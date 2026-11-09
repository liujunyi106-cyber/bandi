"""契约：自动建链只给「计划里实际提到」的角色建子卡，纯问答不得生成空待办。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig, DispatcherReport
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _report() -> DispatcherReport:
    return DispatcherReport(tick_id="t1", started_at=datetime.now(UTC))


class _Org:
    """功能：只提供 get_role 的最小组织替身（建链按角色名匹配计划文本）。"""

    def get_role(self, role_id: str) -> Any:
        names = {"aid_visual": "视觉设计师", "aid_review": "设计评审员"}
        return SimpleNamespace(name=names.get(role_id, role_id), responsibility="交付")


def _mk_dispatcher(tmp_path: Path) -> tuple[CompanyDispatcher, WorkItemStore]:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=_Org(),  # type: ignore[arg-type]
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    dispatcher.task_preparer = None
    return dispatcher, store


def _approved_parent(store: WorkItemStore, output: str) -> Any:
    item = create_work_item(
        title="纯问答",
        turn_type=WorkItemTurnType.EXECUTE,
        assignee_role="aid_requirements",
        phase=Phase.APPROVED,
        metadata={"allowed_delegate_role_ids": ["aid_visual", "aid_review"]},
    )
    item.runtime_metadata["_last_output"] = output
    store.save(item)
    return store.get(item.id)


def test_plain_answer_builds_no_chain(tmp_path: Path) -> None:
    dispatcher, store = _mk_dispatcher(tmp_path)
    parent = _approved_parent(store, "OK")
    assert parent is not None

    dispatcher._ensure_chain_for_completed_planner(_report())

    children = [item for item in store.list_all() if item.parent_id == parent.id]
    assert children == [], f"纯问答不得生成子卡（实际 {[c.title for c in children]}）"
    assert (store.get(parent.id).metadata or {}).get("_auto_chain_built") is True


def test_plain_answer_does_not_requeue_dispatch_guard(tmp_path: Path) -> None:
    dispatcher, store = _mk_dispatcher(tmp_path)
    parent = _approved_parent(store, "OK")
    assert parent is not None

    item, retry = dispatcher._apply_dispatch_guard(
        item=parent,
        output="OK",
        pre_dependency_ids=set(),
        pre_child_ids=set(),
    )

    assert retry is False, "纯问答未提角色不得 requeue（否则同轮重复调用 LLM）"
    assert not (item.metadata or {}).get("manager_dispatch_guard_attempts")


def test_plan_mentioning_role_builds_only_that_chain(tmp_path: Path) -> None:
    dispatcher, store = _mk_dispatcher(tmp_path)
    parent = _approved_parent(store, "| 视觉设计师 | 输出视觉稿 |\n| 其他 | 不参与 |")
    assert parent is not None

    dispatcher._ensure_chain_for_completed_planner(_report())

    children = [item for item in store.list_all() if item.parent_id == parent.id]
    assert [child.assignee_role for child in children] == ["aid_visual"]
    assert "视觉稿" in children[0].description

    dispatcher, store = _mk_dispatcher(tmp_path)
    parent = _approved_parent(store, "| 视觉设计师 | 输出视觉稿 |\n| 其他 | 不参与 |")
    assert parent is not None

    dispatcher._ensure_chain_for_completed_planner(_report())

    children = [item for item in store.list_all() if item.parent_id == parent.id]
    assert [child.assignee_role for child in children] == ["aid_visual"]
    assert "视觉稿" in children[0].description
