"""契约族：组织持久化失败显式化。

生产调用路径：
1. OrgEngine._persist——角色/成员变更后落盘（失败不得静默吞）
2. OrgEngine.persist_health——健康状态（dispatcher/UI/审计消费）
3. CompanyDispatcher.tick——degraded 时 report.errors 告警

断言「最终产物」：持久化失败暴露 failed_count/last_error / 连续失败达阈值
degraded / dispatcher report 携带 org_persist 健康信息。
"""

from __future__ import annotations

import asyncio
from typing import Any

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.store import WorkItemStore


def _mk_dispatcher(store: WorkItemStore, org: OrgEngine) -> CompanyDispatcher:
    return CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=org,
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )


def test_persist_failure_surfaces_health() -> None:
    """持久化抛错 → failed_count/last_error 暴露，不静默。"""

    class FailingStore:
        def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
            raise RuntimeError("disk full")

        def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
            raise RuntimeError("disk full")

        def load_org_roles(self) -> list:
            return []

        def load_org_employees(self) -> list:
            return []

    org = OrgEngine(persist_store=FailingStore())
    org.register_role("r1", "R1", responsibility="x")
    health = org.persist_health()
    assert health["healthy"] is False
    assert health["failed_count"] >= 1
    assert "disk full" in health["last_error"]


def test_persist_degraded_after_threshold() -> None:
    """连续失败达阈值 → degraded 标志。"""

    class FailingStore:
        def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
            raise RuntimeError("boom")

        def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
            raise RuntimeError("boom")

        def load_org_roles(self) -> list:
            return []

        def load_org_employees(self) -> list:
            return []

    org = OrgEngine(persist_store=FailingStore())
    for i in range(4):
        org.register_role(f"r{i}", f"R{i}", responsibility="x")
    health = org.persist_health()
    assert health["degraded"] is True, "连续失败达阈值必须标记 degraded"


def test_persist_recovery_clears_degraded() -> None:
    """后续成功清除 failed_count/degraded。"""

    class FlakyStore:
        def __init__(self) -> None:
            self.fail = True

        def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
            if self.fail:
                raise RuntimeError("boom")
            self.saved = True

        def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
            if self.fail:
                raise RuntimeError("boom")

        def load_org_roles(self) -> list:
            return []

        def load_org_employees(self) -> list:
            return []

    st = FlakyStore()
    org = OrgEngine(persist_store=st)
    for _ in range(3):
        org.register_role("r", "R", responsibility="x")
    assert org.persist_health()["degraded"] is True
    st.fail = False
    org.register_role("r2", "R2", responsibility="x")
    health = org.persist_health()
    assert health["healthy"] is True
    assert health["degraded"] is False
    assert health["failed_count"] == 0


def test_dispatcher_report_carries_persist_health() -> None:
    """tick 报告携带 org_persist 健康信息（degraded 时 errors 告警）。"""
    import tempfile

    from bandi.company.phase import Phase
    from bandi.company.work_item import WorkItemTurnType, create_work_item

    tmp = tempfile.mkdtemp()
    store = WorkItemStore(f"{tmp}/wi.db")
    item = create_work_item(title="t", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)

    class FailingStore:
        def save_org_roles(self, roles: list[dict[str, Any]]) -> None:
            raise RuntimeError("boom")

        def save_org_employees(self, employees: list[dict[str, Any]]) -> None:
            raise RuntimeError("boom")

        def load_org_roles(self) -> list:
            return []

        def load_org_employees(self) -> list:
            return []

    org = OrgEngine(persist_store=FailingStore())
    for i in range(3):
        org.register_role(f"r{i}", f"R{i}", responsibility="x")
    disp = _mk_dispatcher(store, org)
    report = asyncio.run(disp.tick())
    assert "org_persist" in report.metadata
    assert report.metadata["org_persist"]["degraded"] is True
    assert any("org persist degraded" in e for e in report.errors)


def test_persist_callable_degradation_surfaces() -> None:
    """persist_callable（org.json 通道）失败同样计数 degraded——生产 OrgEngine
    无 persist_store 时由回调承接持久化健康。"""
    calls: list[int] = []

    def failing_callable() -> None:
        calls.append(1)
        raise RuntimeError("org.json disk full")

    org = OrgEngine(persist_callable=failing_callable)
    for i in range(3):
        org.register_role(f"r{i}", f"R{i}", responsibility="x")
    health = org.persist_health()
    assert len(calls) == 3, "回调必须每次变更都触发"
    assert health["degraded"] is True, "回调失败达阈值必须 degraded"
    assert "disk full" in health["last_error"]

    # 恢复
    def ok_callable() -> None:
        calls.append(1)

    org._persist_callable = ok_callable  # type: ignore[assignment]
    org.register_role("r9", "R9", responsibility="x")
    health2 = org.persist_health()
    assert health2["healthy"] is True
    assert health2["degraded"] is False
