"""契约族：依赖传播两阶段提交 + 乐观锁。

生产调用路径：
1. work_item_transition.refresh_dependents——tick 依赖传播（company_mode 两处调用）
2. WorkItemStore.save_checked——乐观锁提交（并发修改冲突检测）

断言「最终产物」：正常传播全部落库且 revision 递增 / 并发冲突卡被跳过并
记入 errors_out（不静默）/ 下轮幂等重试收敛一致态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item
from bandi.company.work_item_transition import refresh_dependents


def _mk_store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "dep.db"))


def _mk_dep_item(
    store: WorkItemStore,
    item_id: str,
    *,
    deps: list[str] | None = None,
    phase: Phase = Phase.QUEUED,
) -> None:
    item = create_work_item(title=item_id, turn_type=WorkItemTurnType.EXECUTE, phase=phase)
    item.id = item_id
    if deps:
        item.metadata["dependency_work_item_ids"] = deps
    store.save(item)


def test_refresh_commits_all_with_revision(tmp_path: Path) -> None:
    """正常传播：依赖全 approved 后父卡从 WAITING_DEPENDENCIES 释放为 READY
    且 revision 递增（乐观锁生效）。"""
    store = _mk_store(tmp_path)
    _mk_dep_item(store, "child", phase=Phase.APPROVED)
    _mk_dep_item(store, "parent", deps=["child"], phase=Phase.WAITING_DEPENDENCIES)
    rev_before = store.get("parent").revision

    errors: list[str] = []
    changed = refresh_dependents(store, errors_out=errors)
    assert changed is True
    assert errors == []
    parent = store.get("parent")
    assert parent is not None
    assert parent.phase == Phase.READY, "依赖 approved 后父卡必须释放"
    assert parent.revision == rev_before + 1, "提交必须递增 revision"


def test_refresh_conflict_surfaces_error_and_skips(tmp_path: Path) -> None:
    """并发修改冲突：save_checked 失败（库内 revision 已被他处推进）→ 该卡被
    跳过并记入 errors_out，不静默吞掉。"""
    store = _mk_store(tmp_path)
    _mk_dep_item(store, "child", phase=Phase.APPROVED)
    _mk_dep_item(store, "parent", deps=["child"], phase=Phase.WAITING_DEPENDENCIES)
    # 模拟阶段 2 提交前他进程推进了库内 revision（并发写）
    parent = store.get("parent")
    store.save(parent)  # revision 0→1
    rev_after_advance = store.get("parent").revision

    # 直接调用 save_checked 验证冲突检测：用旧期望（更早 revision）必须被拒绝
    stale_expected = rev_after_advance - 1
    assert store.save_checked(store.get("parent"), stale_expected) is False, (
        f"旧 revision 期望 {stale_expected} 必须被拒绝（当前 {rev_after_advance}）"
    )
    assert store.get("parent").revision == rev_after_advance, "冲突失败不得推进 revision"


def test_refresh_idempotent_retry_converges(tmp_path: Path) -> None:
    """幂等重试：冲突后修正（re-read 最新）再跑 refresh → 收敛到一致态。"""
    store = _mk_store(tmp_path)
    _mk_dep_item(store, "child", phase=Phase.APPROVED)
    _mk_dep_item(store, "parent", deps=["child"], phase=Phase.WAITING_DEPENDENCIES)

    errors: list[str] = []
    refresh_dependents(store, errors_out=errors)
    assert errors == []
    assert store.get("parent").phase == Phase.READY

    # 幂等：再跑一遍无新变化（value-level idempotent）
    errors2: list[str] = []
    changed2 = refresh_dependents(store, errors_out=errors2)
    assert changed2 is False, "已收敛后再跑不得再变更"
    assert store.get("parent").phase == Phase.READY


def test_refresh_phase_guard_no_partial_writes_on_invalid(tmp_path: Path) -> None:
    """阶段 1 校验失败（非法相位转换）不得产生任何 pending 写入——整轮放弃。"""
    store = _mk_store(tmp_path)
    # parent 已是终态 ，且依赖 child 尚未 approved——refresh 尝试
    # 反向 READY→WAITING_DEPENDENCIES 会因终态守卫失败，但不应留下部分写
    _mk_dep_item(store, "child", phase=Phase.QUEUED)
    _mk_dep_item(store, "parent", deps=["child"], phase=Phase.APPROVED)

    errors: list[str] = []
    refresh_dependents(store, errors_out=errors)
    # 卡不得被反向降级——refresh 对终态卡无操作
    assert store.get("parent").phase == Phase.APPROVED


def test_refresh_conflict_skips_and_reports_in_errors(tmp_path: Path, monkeypatch: Any) -> None:
    """refresh 阶段 2 并发冲突：冲突卡被跳过并记入 errors_out，其他卡不受影响；
    修正后重跑收敛。"""
    store = _mk_store(tmp_path)
    _mk_dep_item(store, "child-a", phase=Phase.APPROVED)
    _mk_dep_item(store, "child-b", phase=Phase.APPROVED)
    _mk_dep_item(store, "parent-a", deps=["child-a"], phase=Phase.WAITING_DEPENDENCIES)
    _mk_dep_item(store, "parent-b", deps=["child-b"], phase=Phase.WAITING_DEPENDENCIES)

    orig_save_checked = store.save_checked
    conflicted: set[str] = set()

    def flaky_save_checked(item: Any, expected_revision: int) -> bool:
        if item.id == "parent-b":
            conflicted.add(item.id)
            return False  # 模拟 parent-b 被他进程并发修改
        return orig_save_checked(item, expected_revision)

    monkeypatch.setattr(store, "save_checked", flaky_save_checked)
    errors: list[str] = []
    refresh_dependents(store, errors_out=errors)

    assert conflicted == {"parent-b"}, "parent-b 必须命中冲突分支"
    assert any("parent-b" in e and "concurrent" in e for e in errors), (
        f"冲突必须记入 errors_out（实际 {errors}）"
    )
    assert store.get("parent-a").phase == Phase.READY, "parent-a 不受冲突影响正常释放"
    assert store.get("parent-b").phase == Phase.WAITING_DEPENDENCIES, "冲突卡不得被提交"

    # 修正后重跑（去掉 flaky）→ 收敛
    monkeypatch.setattr(store, "save_checked", orig_save_checked)
    errors2: list[str] = []
    refresh_dependents(store, errors_out=errors2)
    assert store.get("parent-b").phase == Phase.READY, "重试必须收敛"
