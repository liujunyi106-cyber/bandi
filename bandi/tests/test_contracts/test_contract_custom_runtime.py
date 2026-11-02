# 功能：custom_runtime 崩溃恢复接线行为契约（防 fix13 未完成项——装配点遗漏复发）
# 契约原则：build_org_runtime 装配的 dispatcher 必须与 CLI/API 启动链路一致
# 在首次 tick 前 recover stale in-flight 卡；钉行为结果不钉实现（不 grep 源码）。
from __future__ import annotations

from bandi.company.company_runtime_profiles import RoleProfile
from bandi.company.custom_runtime import CustomRuntimeRunner, RuntimeHost
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _roles() -> list[RoleProfile]:
    return [
        RoleProfile(id="manager", name="Manager", reports_to=None, can_spawn=["worker"]),
        RoleProfile(id="worker", name="Worker", reports_to="manager"),
    ]


def _stale_item(store: WorkItemStore, title: str = "stale running") -> str:
    item = create_work_item(
        title=title,
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.RUNNING,
        assignee_role="worker",
        metadata={"runtime_owner": "custom:custom"},
    )
    store.save(item)
    return item.id


def test_contract_build_org_runtime_recovers_stale_in_flight(tmp_path) -> None:
    """契约 1：build_org_runtime 装配后 stale RUNNING 卡必须转 READY——
    与 CLI/API 启动链路（cli/company.py:85 / api/app.py:505）语义一致
    否则 custom_runtime 装配的调度器永远不认领崩溃遗留卡。"""
    store = WorkItemStore(str(tmp_path / "custom.db"))
    stale_id = _stale_item(store)
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1"))

    runner.build_org_runtime(roles=_roles())

    loaded = store.get(stale_id)
    assert loaded is not None
    assert loaded.phase == Phase.READY, (
        f"build_org_runtime 后崩溃遗留 RUNNING 卡必须恢复为 READY，实际 {loaded.phase.value}"
    )


def test_contract_recover_idempotent_across_rebuilds(tmp_path) -> None:
    """契约 2：recover 幂等——已 READY 的卡跨多次 build 不得重复转（无副作用）。"""
    store = WorkItemStore(str(tmp_path / "custom.db"))
    stale_id = _stale_item(store)
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1"))

    runner.build_org_runtime(roles=_roles())
    runner.build_org_runtime(roles=_roles())

    loaded = store.get(stale_id)
    assert loaded is not None and loaded.phase == Phase.READY


def test_contract_review_phases_untouched_by_build(tmp_path) -> None:
    """契约 3：评审态（AWAITING_*）不得被 build 触发 recover 破坏——
    对齐 recover_stale_claims 语义（评审链推进），build 不得误转 READY。"""
    store = WorkItemStore(str(tmp_path / "custom.db"))
    review_item = create_work_item(
        title="awaiting review",
        turn_type=WorkItemTurnType.EXECUTE,
        phase=Phase.AWAITING_MANAGER_REVIEW,
    )
    store.save(review_item)
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1"))

    runner.build_org_runtime(roles=_roles())

    loaded = store.get(review_item.id)
    assert loaded is not None and loaded.phase == Phase.AWAITING_MANAGER_REVIEW


def test_contract_all_stale_recovered_in_single_build(tmp_path) -> None:
    """契约 4：多张 stale 卡一次 build 全部恢复（不遗漏个别卡）。"""
    store = WorkItemStore(str(tmp_path / "custom.db"))
    ids = [_stale_item(store, f"stale-{i}") for i in range(3)]
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="p1"))

    runner.build_org_runtime(roles=_roles())

    for item_id in ids:
        loaded = store.get(item_id)
        assert loaded is not None and loaded.phase == Phase.READY, f"{item_id} 未恢复"


def test_contract_empty_scope_falls_back_to_default(tmp_path) -> None:
    """契约 5：无会话上下文宿主（project_id/session_id 空）装配必须回落 default 隔离
    作用域——comms._safe_path_segment fail-closed 拒绝空段，透传空串会延迟到 send
    才崩（装配能成、发消息炸）。"""
    store = WorkItemStore(str(tmp_path / "custom.db"))
    runner = CustomRuntimeRunner(RuntimeHost(store=store, project_id="", session_id=""))

    bundle = runner.build_org_runtime(roles=_roles())

    assert bundle.communication.project_id == "default"
    assert bundle.communication.session_id == "default"
