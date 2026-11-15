from __future__ import annotations

import pytest

from bandi.company.company_runtime import CompanyRuntime
from bandi.company.seat_executor import EngineSeatExecutor
from bandi.company.work_item import WorkItemTurnType, create_work_item


def _runtime_and_item(*, background: bool):
    runtime = CompanyRuntime(project_id="p", session_scope_id="s", role_id="r", employee_id="e")
    item = create_work_item(title="bg", turn_type=WorkItemTurnType.EXECUTE)
    if background:
        item.metadata["background_run"] = True
    return runtime, item


@pytest.mark.asyncio
async def test_assistant_background_allows_any_tool() -> None:
    from bandi.api.app import _BackgroundInteraction

    ui = _BackgroundInteraction()
    assert await ui.ask_permission("write", {}, "high") is True
    assert await ui.ask_permission("bash", {}, "high") is True
    assert await ui.ask_confirmation("delete?") is True
    assert await ui.ask_input("name?") == ""


@pytest.mark.asyncio
async def test_employee_background_run_uses_auto_approve(monkeypatch) -> None:
    from bandi.core.user_interaction import AutoApproveInteraction

    captured: dict[str, object] = {}

    async def fake_run_agent(**kwargs):
        captured["user_interaction"] = kwargs.get("user_interaction")
        return "ok", "VERIFIED", 1, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    sentinel = object()
    executor = EngineSeatExecutor(interaction_provider=lambda session_id: sentinel)
    runtime, item = _runtime_and_item(background=True)

    await executor.run_turn(runtime, item)

    assert isinstance(captured["user_interaction"], AutoApproveInteraction)


@pytest.mark.asyncio
async def test_employee_normal_run_keeps_interaction_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_agent(**kwargs):
        captured["user_interaction"] = kwargs.get("user_interaction")
        return "ok", "VERIFIED", 1, 1

    monkeypatch.setattr("bandi.company.seat_executor.run_agent", fake_run_agent)
    sentinel = object()
    executor = EngineSeatExecutor(interaction_provider=lambda session_id: sentinel)
    runtime, item = _runtime_and_item(background=False)

    await executor.run_turn(runtime, item)

    assert captured["user_interaction"] is sentinel


@pytest.mark.asyncio
async def test_employee_scheduled_task_marks_background_run(monkeypatch) -> None:
    import sys

    from bandi.company.work_item import create_work_item as _create

    app_module = sys.modules["bandi.api.app"]
    item = _create(title="scheduled", turn_type=WorkItemTurnType.EXECUTE)
    saved: list[object] = []

    class _FakeStore:
        def save(self, work_item) -> None:
            saved.append(work_item)

    class _FakeDispatcher:
        org_engine = object()
        store = _FakeStore()

        def __init__(self) -> None:
            self.claims: list[str] = []
            self.releases: list[str] = []

        def claim(self, role_id: str) -> bool:
            self.claims.append(role_id)
            return True

        def release(self, role_id: str) -> bool:
            self.releases.append(role_id)
            return True

        async def run_work_item(self, work_item_id: str):
            class _Result:
                output = "done"

            return _Result()

    dispatcher = _FakeDispatcher()

    class _FakeSessionStore:
        def append_message(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(app_module, "_get_company_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(app_module, "_get_project_comms", lambda project_id: object())
    monkeypatch.setattr(app_module, "_get_project_threads", lambda project_id: object())
    monkeypatch.setattr(
        "bandi.api.company_chat._resolve_registered_role",
        lambda employee_id, org_engine: employee_id,
    )
    monkeypatch.setattr(
        "bandi.api.company_chat.handle_chat_message",
        lambda **kwargs: [item],
    )

    output = await app_module._run_employee_scheduled_task(
        task=type("Task", (), {"payload": {}, "name": "t", "prompt": "p"})(),
        session_id="sid",
        project_id="pid",
        employee_id="emp",
        prompt="p",
        session_store=_FakeSessionStore(),
    )

    assert output == "done"
    assert dispatcher.claims == ["emp"]
    assert dispatcher.releases == ["emp"]
    assert item.metadata.get("background_run") is True
    assert saved == [item]
