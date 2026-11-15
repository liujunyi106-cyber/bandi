# 功能：认领/续约必须递增 revision，防止旧快照 save_checked 覆盖租约与 RUNNING
from __future__ import annotations

from pathlib import Path

from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItemTurnType, create_work_item


def test_claim_invalidates_stale_save_checked(tmp_path: Path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    item = create_work_item("t", WorkItemTurnType.EXECUTE, phase=Phase.READY)
    store.save(item)
    stale = store.get(item.id)
    assert stale is not None

    assert store.claim_for_execution(item.id, "instance-a", 60) is True
    claimed = store.get(item.id)
    assert claimed is not None
    assert claimed.phase == Phase.RUNNING
    assert claimed.revision > stale.revision

    stale.phase = Phase.APPROVED
    assert store.save_checked(stale, stale.revision) is False
    after = store.get(item.id)
    assert after is not None
    assert after.phase == Phase.RUNNING
    assert after.metadata.get("execution_claim", {}).get("instance_id") == "instance-a"
    store.close()


def test_release_invalidates_stale_save_checked(tmp_path: Path) -> None:
    store = WorkItemStore(str(tmp_path / "wi.db"))
    item = create_work_item("t", WorkItemTurnType.EXECUTE, phase=Phase.READY)
    store.save(item)
    assert store.claim_for_execution(item.id, "instance-a", 60) is True
    stale = store.get(item.id)
    assert stale is not None
    store.release_execution_claim(item.id, instance_id="instance-a")

    stale.phase = Phase.APPROVED
    assert store.save_checked(stale, stale.revision) is False
    store.close()
