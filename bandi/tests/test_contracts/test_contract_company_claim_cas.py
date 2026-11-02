"""契约族：work item 数据库级 CAS 认领+ 租约续期。

生产调用路径：
1. WorkItemStore.claim_for_execution——dispatcher _try_claim_work_item 原子认领
2. WorkItemStore.renew_execution_lease——run_work_item 长任务期间租约续期
3. CompanyDispatcher._try_claim_work_item——claim→run→release 闭环入口
4. recover_stale_claims——续期后的有效租约不得被接管

断言「最终产物」：并发 CAS 仅一方成功 / 续期后 expires_at 刷新 / 他进程
见到有效租约不接管。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item

_INSTANCE_A = "inst-a"
_INSTANCE_B = "inst-b"
_LEASE_SEC = 120


def _mk_store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _mk_ready_item(store: WorkItemStore) -> WorkItem:
    item = create_work_item(title="t1", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)
    return store.get(item.id)  # type: ignore[return-value]


def _write_claim(store: WorkItemStore, item_id: str, instance: str, lease_sec: int) -> None:
    item = store.get(item_id)
    assert item is not None
    now = datetime.now(UTC)
    item.metadata["execution_claim"] = {
        "instance_id": instance,
        "claimed_at": now.isoformat(),
        "lease_expires_at": (now + timedelta(seconds=lease_sec)).isoformat(),
    }
    store.save(item)


def test_claim_cas_two_instances_only_one_wins(tmp_path: Path) -> None:
    """模拟两进程读同一 READY 卡：A 先认领后，B 再 claim 必须失败（CAS 行数 0）。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True
    assert store.claim_for_execution(item.id, _INSTANCE_B, _LEASE_SEC) is False, (
        "B 不得在 A 持有有效租约时认领同一卡"
    )


def test_claim_cas_phase_guard(tmp_path: Path) -> None:
    """RUNNING 卡不得被重新认领（CAS 期望 phase=READY，实际已是 running → 拒绝）。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True
    item2 = store.get(item.id)
    assert item2 is not None
    item2.metadata.pop("execution_claim", None)
    store.save(item2)
    assert store.claim_for_execution(item.id, _INSTANCE_B, _LEASE_SEC) is False, (
        "RUNNING 卡不得被重新认领"
    )


def test_claim_preserves_concurrent_metadata(tmp_path: Path) -> None:
    """认领写入租约时不得抹掉卡上其他并发字段（owner 等窗口期写入）。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    # 模拟窗口期他进程在卡上写入 owner 字段（认领前已存在）
    item2 = store.get(item.id)
    assert item2 is not None
    item2.metadata["owner"] = "other"
    store.save(item2)
    # 认领成功（无他实例租约）
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True
    claimed = store.get(item.id)
    assert claimed is not None
    assert claimed.metadata["owner"] == "other", "认领不得覆盖并发写入的 owner 字段"
    assert claimed.metadata["execution_claim"]["instance_id"] == _INSTANCE_A


def test_lease_renews_expires_at(tmp_path: Path) -> None:
    """续约刷新 expires_at：续约后租约有效期延长，且仅本实例可续。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True
    first = store.get(item.id)
    assert first is not None
    first_expiry = first.metadata["execution_claim"]["lease_expires_at"]
    assert store.renew_execution_lease(item.id, _INSTANCE_B, _LEASE_SEC) is False
    assert store.renew_execution_lease(item.id, _INSTANCE_A, _LEASE_SEC) is True
    second = store.get(item.id)
    assert second is not None
    second_expiry = second.metadata["execution_claim"]["lease_expires_at"]
    assert second_expiry >= first_expiry, "续约不得缩短有效期"
    assert datetime.fromisoformat(second_expiry) > datetime.now(UTC), "续约后租约必须仍有效"


def test_renew_lost_after_reclaim(tmp_path: Path) -> None:
    """他进程已接管（租约被覆盖）后，本实例续约必须失败。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True
    item2 = store.get(item.id)
    assert item2 is not None
    item2.metadata["execution_claim"]["instance_id"] = _INSTANCE_B
    item2.metadata["execution_claim"]["lease_expires_at"] = (
        datetime.now(UTC) + timedelta(seconds=_LEASE_SEC)
    ).isoformat()
    store.save(item2)
    assert store.renew_execution_lease(item.id, _INSTANCE_A, _LEASE_SEC) is False, (
        "租约已被他实例接管，A 续约必须失败"
    )


def test_recover_skips_renewed_lease(tmp_path: Path) -> None:
    """续期后的有效租约，recover_stale_claims 不得接管。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True
    store.renew_execution_lease(item.id, _INSTANCE_A, _LEASE_SEC)

    org = OrgEngine()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=org,
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    dispatcher._in_flight_work_item_ids = set()  # 模拟另一进程无内存登记
    # recover 扫描：A 有效租约未过期 → 本进程不接管
    assert dispatcher.recover_stale_claims() == 0
    item2 = store.get(item.id)
    assert item2 is not None
    assert item2.phase == Phase.RUNNING, "有效租约卡不得被 recover 转 READY"


def test_expired_lease_can_be_reclaimed(tmp_path: Path) -> None:
    """过期租约可被接管：A 崩溃遗留过期租约，B 可 claim。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, 1) is True
    # 租约过期：手写过期时间
    item2 = store.get(item.id)
    assert item2 is not None
    item2.metadata["execution_claim"]["lease_expires_at"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    ).isoformat()
    store.save(item2)
    item3 = store.get(item.id)
    assert item3 is not None
    item3.phase = Phase.READY
    store.save(item3)
    assert store.claim_for_execution(item.id, _INSTANCE_B, _LEASE_SEC) is True


def _mk_queued_item(store: WorkItemStore) -> WorkItem:
    item = create_work_item(title="q1", turn_type=WorkItemTurnType.EXECUTE)
    store.save(item)
    return store.get(item.id)  # type: ignore[return-value]


def test_claim_from_queued_transitions_to_running(tmp_path: Path) -> None:
    """QUEUED 卡经 claim_from_queued 一步转 RUNNING + 写租约。"""
    store = _mk_store(tmp_path)
    item = _mk_queued_item(store)
    assert store.claim_from_queued(item.id, _INSTANCE_A, _LEASE_SEC) is True
    claimed = store.get(item.id)
    assert claimed is not None
    assert claimed.phase == Phase.RUNNING
    assert claimed.metadata["execution_claim"]["instance_id"] == _INSTANCE_A


def test_claim_from_queued_rejects_already_claimed(tmp_path: Path) -> None:
    """他进程已认领（RUNNING+租约）的卡，claim_from_queued 必须拒绝且不覆盖。"""
    store = _mk_store(tmp_path)
    item = _mk_queued_item(store)
    assert store.claim_from_queued(item.id, _INSTANCE_A, _LEASE_SEC) is True
    assert store.claim_from_queued(item.id, _INSTANCE_B, _LEASE_SEC) is False
    final = store.get(item.id)
    assert final is not None
    assert final.phase == Phase.RUNNING, "B 的 claim_from_queued 不得把卡打回"
    assert final.metadata["execution_claim"]["instance_id"] == _INSTANCE_A, "不得覆盖 A 的租约"


def test_claim_from_queued_rejects_non_queued_phase(tmp_path: Path) -> None:
    """非 QUEUED 卡（如 READY）claim_from_queued 拒绝。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_from_queued(item.id, _INSTANCE_A, _LEASE_SEC) is False


def test_run_work_item_queued_does_not_overwrite_foreign_lease(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """回归：A 已认领 QUEUED 卡后，B 的 run_work_item 不得覆盖（pre-promote 回归）。"""
    import asyncio

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.org_engine import OrgEngine
    from bandi.company.seat_executor import EngineSeatExecutor, SeatResult

    class NoopSeat(EngineSeatExecutor):
        async def run_turn(self, runtime: Any, item: Any) -> SeatResult:
            return SeatResult(success=True, output="done")

    store = _mk_store(tmp_path)
    item = _mk_queued_item(store)
    assert store.claim_from_queued(item.id, _INSTANCE_A, _LEASE_SEC) is True

    org = OrgEngine()
    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=org,
        communication=None,
        seat_executor=NoopSeat(),
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    disp.instance_id = _INSTANCE_B
    result = asyncio.run(disp.run_work_item(item.id))
    assert result.success is False, "B 不得执行 A 已认领的卡"
    # 不得覆盖 A 的租约
    final = store.get(item.id)
    assert final is not None
    assert final.phase == Phase.RUNNING
    assert final.metadata["execution_claim"]["instance_id"] == _INSTANCE_A


def test_recover_does_not_overwrite_foreign_reclaim(tmp_path: Path) -> None:
    """recover_stale_claims CAS 化：他进程已接管（库内 revision 变）后，recover
    不得覆盖其状态（回归防护）。"""

    from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
    from bandi.company.org_engine import OrgEngine

    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    assert store.claim_for_execution(item.id, _INSTANCE_A, _LEASE_SEC) is True

    org = OrgEngine()
    disp = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=org,
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    disp.instance_id = _INSTANCE_B
    item2 = store.get(item.id)
    assert item2 is not None
    item2.metadata["execution_claim"]["instance_id"] = _INSTANCE_A
    store.save(item2)  # revision +1，模拟 A 窗口期更新
    disp.recover_stale_claims()  # B 的 recover 应因 revision 冲突跳过
    final = store.get(item.id)
    assert final is not None
    assert final.phase == Phase.RUNNING, "recover 不得覆盖他进程认领的卡"
    assert final.metadata["execution_claim"]["instance_id"] == _INSTANCE_A
