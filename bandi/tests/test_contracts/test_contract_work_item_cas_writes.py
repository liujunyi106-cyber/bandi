# 功能：契约——work item 字段回写 CAS 化（家族：非 CAS save 覆盖他进程字段）。
# 钉行为：基于旧快照的字段回写（执行解析/结果/中断/员工指派）经 save_checked +
# 冲突重放——他进程窗口期写入的字段不得被覆盖；持续冲突时不回写旧快照（保他人数据）。
from __future__ import annotations

from pathlib import Path

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig, _apply_and_save_checked
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item


def _stores(tmp_path: Path) -> tuple[WorkItemStore, WorkItemStore]:
    db = str(tmp_path / "wi.db")
    return WorkItemStore(db), WorkItemStore(db)


def _ready_item(store: WorkItemStore) -> WorkItem:
    item = create_work_item(title="CAS 用例", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)
    return store.get(item.id)  # type: ignore[return-value]


def test_apply_and_save_checked_replays_on_conflict(tmp_path: Path) -> None:
    store_a, store_b = _stores(tmp_path)
    item = _ready_item(store_a)

    # 他进程（store_b）先写 probe 字段（revision +1）
    other = store_b.get(item.id)
    assert other is not None
    other.metadata["concurrent_probe"] = "keep-me"
    store_b.save(other)

    # 本进程基于旧快照回写——CAS 冲突后重读重放
    result = _apply_and_save_checked(
        store_a, item, lambda target: target.runtime_metadata.update({"worker_field": "mine"})
    )

    final = store_b.get(item.id)
    assert final is not None
    assert final.metadata.get("concurrent_probe") == "keep-me", "他进程字段不得被覆盖"
    assert final.runtime_metadata.get("worker_field") == "mine", "本进程字段必须落盘"
    assert result.metadata.get("concurrent_probe") == "keep-me"


def test_resolve_execution_agent_survives_concurrent_write(tmp_path: Path) -> None:
    store_a, store_b = _stores(tmp_path)
    item = _ready_item(store_a)
    old_snapshot = store_a.get(item.id)
    assert old_snapshot is not None

    other = store_b.get(item.id)
    assert other is not None
    other.metadata["concurrent_probe"] = "keep-me"
    store_b.save(other)

    dispatcher = CompanyDispatcher(
        store=store_a,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    resolved = dispatcher._resolve_execution_agent(old_snapshot)

    final = store_b.get(item.id)
    assert final is not None
    assert resolved.runtime_metadata.get("effective_execution_agent"), "解析字段必须应用"
    assert final.metadata.get("concurrent_probe") == "keep-me", "旧实现 store.save 会覆盖丢失"
    assert final.runtime_metadata.get("effective_execution_agent"), "解析结果必须落盘"


def test_persistent_conflict_never_overwrites(tmp_path: Path, monkeypatch) -> None:
    store_a, store_b = _stores(tmp_path)
    item = _ready_item(store_a)

    other = store_b.get(item.id)
    assert other is not None
    other.metadata["concurrent_probe"] = "keep-me"
    store_b.save(other)

    monkeypatch.setattr(store_a, "save_checked", lambda _item, _revision: False)
    result = _apply_and_save_checked(
        store_a, item, lambda target: target.runtime_metadata.update({"worker_field": "mine"})
    )

    final = store_b.get(item.id)
    assert final is not None and final.metadata.get("concurrent_probe") == "keep-me"
    assert result.metadata.get("concurrent_probe") == "keep-me", "返回值为重读后的最新快照"
