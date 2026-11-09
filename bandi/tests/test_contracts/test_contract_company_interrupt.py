# 功能：契约——公司链中断（abort_controller 透传到员工执行 + 中断后卡片转人工态）。
# 钉行为：EngineSeatExecutor 经 bind_abort_provider 拿到工作项级 AbortController 并
# 透传 run_agent；CompanyDispatcher.interrupt_work_item 置位执行中卡片的中断信号
# 执行结束后卡片转 NEEDS_ATTENTION 并留中断原因（不得静默丢弃）；无活动执行返回 False。
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bandi.company.company_mode import CompanyDispatcher, DispatcherConfig
from bandi.company.org_engine import OrgEngine
from bandi.company.phase import Phase
from bandi.company.seat_executor import EngineSeatExecutor, SeatResult
from bandi.company.store import WorkItemStore
from bandi.company.work_item import WorkItem, WorkItemTurnType, create_work_item
from bandi.core.abort_controller import AbortController


def _mk_store(tmp_path: Path) -> WorkItemStore:
    return WorkItemStore(str(tmp_path / "wi.db"))


def _mk_ready_item(store: WorkItemStore) -> WorkItem:
    item = create_work_item(title="中断用例", turn_type=WorkItemTurnType.EXECUTE)
    item.phase = Phase.READY
    store.save(item)
    return store.get(item.id)  # type: ignore[return-value]


# EngineSeatExecutor 把 provider 给的 AbortController 透传给 run_agent
def test_engine_seat_executor_forwards_abort_controller() -> None:
    from bandi.company import seat_executor as se_mod

    executor = EngineSeatExecutor()
    controller = AbortController()
    executor.bind_abort_provider(lambda _item_id: controller)

    seen: dict[str, Any] = {}

    async def _spy_run_agent(**kwargs: Any) -> tuple[str, str, int, int]:
        seen.update(kwargs)
        return "完成输出", "COMPLETED", 0, 1

    item = create_work_item(title="透传用例", turn_type=WorkItemTurnType.EXECUTE)
    item.assignee_role = "worker"
    runtime = _fake_runtime()

    with patch.object(se_mod, "run_agent", _spy_run_agent):
        result = asyncio.run(executor.run_turn(runtime, item))

    assert result.success is True
    assert seen.get("abort_controller") is controller, (
        "公司链 run_agent 必须收到工作项 AbortController（E-6 透传）"
    )


def _fake_runtime() -> Any:
    class _Runtime:
        role_id = "worker"
        employee_id = "e1"
        project_id = "p1"

        def member_session_id(self) -> str:
            return "role-runtime::p1::worker"

    return _Runtime()


# dispatcher.run_work_item 执行中 interrupt → 置位信号 + 卡片转 NEEDS_ATTENTION
def test_dispatcher_interrupt_marks_needs_attention(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    blocking = _BlockingSeatExecutor()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        seat_executor=blocking,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    dispatcher.task_preparer = None  # 聚焦中断链路，跳过任务预处理依赖

    async def _run() -> tuple[Any, WorkItem | None]:
        task = asyncio.create_task(dispatcher.run_work_item(item.id))
        for _ in range(300):
            if blocking.seen_abort is not None:
                break
            await asyncio.sleep(0.01)
        assert blocking.seen_abort is not None, "seat 执行必须启动"
        assert await dispatcher.interrupt_work_item(item.id) is True
        await task
        return store.get(item.id), blocking.seen_abort

    final, controller = asyncio.run(_run())

    assert controller.should_abort is True
    assert final is not None
    assert final.phase == Phase.NEEDS_ATTENTION, f"中断后卡片必须转人工态（实际 {final.phase}）"
    assert final.runtime_metadata.get("interrupted_reason"), "中断原因必须落卡（可追溯）"


# 卡在不可轮询 await 的执行也必须被 abort 终止（不能只置 controller）
def test_dispatcher_interrupt_cancels_stuck_execution(tmp_path: Path) -> None:
    """破坏者：executor 卡在永不返回的 await（不检查 abort controller）时，
    interrupt 必须取消执行协程，卡片转 NEEDS_ATTENTION 且释放执行租约。"""
    store = _mk_store(tmp_path)
    item = _mk_ready_item(store)
    stuck = _StuckSeatExecutor()
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        seat_executor=stuck,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )
    dispatcher.task_preparer = None

    cleaned: list[str] = []
    dispatcher.employee_interaction_cleanup = cleaned.append

    async def _run() -> WorkItem | None:
        task = asyncio.create_task(dispatcher.run_work_item(item.id))
        await asyncio.wait_for(stuck.started.wait(), timeout=3)
        assert await dispatcher.interrupt_work_item(item.id) is True
        await asyncio.wait_for(task, timeout=5)
        return store.get(item.id)

    final = asyncio.run(_run())
    assert final is not None
    assert final.phase == Phase.NEEDS_ATTENTION, f"卡死执行中断后必须转人工态（实际 {final.phase}）"
    assert final.runtime_metadata.get("interrupted_reason") == "user_interrupt"
    assert not (final.metadata or {}).get("execution_claim"), "中断后必须释放执行租约"
    assert cleaned, "中断必须关闭挂起的员工交互（否则前端卡片永久等待）"


# 无活动执行时 interrupt_work_item 返回 False（不误报）
def test_interrupt_without_active_execution_returns_false(tmp_path: Path) -> None:
    store = _mk_store(tmp_path)
    dispatcher = CompanyDispatcher(
        store=store,
        recruiter=None,  # type: ignore[arg-type]
        org_engine=OrgEngine(),
        communication=None,
        config=DispatcherConfig(max_concurrent=1, enable_auto_process=False),
    )

    assert asyncio.run(dispatcher.interrupt_work_item("not-running")) is False


class _BlockingSeatExecutor:
    """功能：阻塞型 seat 执行器——拿到 controller 后轮询直到中断置位。"""

    def __init__(self) -> None:
        self.seen_abort: AbortController | None = None
        self._abort_provider: Any = None

    def bind_abort_provider(self, provider: Any) -> None:
        self._abort_provider = provider

    async def prepare_seat(self, runtime: Any, _work_item: WorkItem) -> None:
        return None

    async def run_turn(self, runtime: Any, work_item: WorkItem) -> SeatResult:
        controller = self._abort_provider(str(work_item.id)) if self._abort_provider else None
        self.seen_abort = controller
        while controller is not None and not controller.should_abort:
            await asyncio.sleep(0.01)
        return SeatResult(success=False, output="interrupted by user")

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        return None


class _StuckSeatExecutor:
    """功能：卡死型 seat 执行器——卡在永不返回的 await，不轮询 abort 信号。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self._abort_provider: Any = None

    def bind_abort_provider(self, provider: Any) -> None:
        self._abort_provider = provider

    async def prepare_seat(self, runtime: Any, _work_item: WorkItem) -> None:
        return None

    async def run_turn(self, runtime: Any, work_item: WorkItem) -> SeatResult:
        self.started.set()
        await self.gate.wait()
        return SeatResult(success=True, output="never reached")

    async def checkpoint(self, runtime: Any) -> dict[str, Any]:
        return {}

    async def interrupt(self, runtime: Any) -> None:
        return None


# API 端点接线——无活动 404；有活动 200 且置位
def test_api_abort_work_item_endpoint(monkeypatch) -> None:
    import importlib

    from fastapi.testclient import TestClient

    app_module = importlib.import_module("bandi.api.app")
    client = TestClient(app_module.app)
    client.headers["Authorization"] = "Bearer test"

    class _FakeDispatcher:
        def __init__(self) -> None:
            self.interrupted: list[str] = []

        async def interrupt_work_item(self, item_id: str) -> bool:
            self.interrupted.append(item_id)
            return item_id == "running-1"

    fake = _FakeDispatcher()
    with (
        patch.object(app_module, "_API_KEY", "test"),
        patch.object(app_module, "_get_company_dispatcher", lambda: fake),
    ):
        ok = client.post("/api/company/work-items/running-1/abort")
        missing = client.post("/api/company/work-items/ghost/abort")

    assert ok.status_code == 200 and ok.json().get("interrupted") is True
    assert missing.status_code == 404
    assert fake.interrupted == ["running-1", "ghost"]
