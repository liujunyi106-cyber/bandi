from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.core.agent_runner import _filter_mcp_tools
from bandi.scheduler.scheduler import TaskScheduler
from bandi.scheduler.store import ScheduledTask, TaskStore
from bandi.tools.registry import ToolRegistry

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._task_store", TaskStore(":memory:")),
        patch("bandi.api.app._task_runner", None),
        patch("bandi.api.app._task_scheduler", None),
    ):
        yield


def test_store_persists_periodic_spec_and_next_run() -> None:
    store = TaskStore(":memory:")
    task = ScheduledTask(
        task_id="st-periodic",
        name="daily",
        cron="",
        prompt="p",
        schedule_kind="periodic",
        schedule_spec={"unit": "daily", "hour": 9, "minute": 0},
        effective_from="2026-11-01",
        effective_until="2026-12-31",
    )
    saved = store.save(task, now=datetime(2026, 10, 30, 15, 0))
    assert saved.schedule_kind == "periodic"
    assert saved.schedule_spec == {"unit": "daily", "hour": 9, "minute": 0}
    assert saved.effective_from == "2026-11-01"
    assert saved.next_run_at == datetime(2026, 11, 1, 9, 0)
    reloaded = store.get("st-periodic")
    assert reloaded is not None and reloaded.schedule_spec == saved.schedule_spec


def test_create_and_patch_schedule_model_api() -> None:
    created = client.post(
        "/api/scheduler/tasks",
        json={
            "name": "once",
            "prompt": "p",
            "schedule_kind": "once",
            "schedule_spec": {"date": "2099-01-02", "hour": 10, "minute": 30},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["schedule_kind"] == "once"
    assert body["schedule_spec"]["date"] == "2099-01-02"
    assert body["next_run_at"]

    patched = client.patch(
        f"/api/scheduler/tasks/{body['task_id']}",
        json={
            "schedule_kind": "periodic",
            "schedule_spec": {"unit": "weekly", "weekday": 1, "hour": 8, "minute": 0},
        },
    )
    assert patched.status_code == 200
    assert patched.json()["schedule_kind"] == "periodic"
    assert patched.json()["schedule_spec"]["unit"] == "weekly"


def test_task_mcp_server_allowlist_roundtrip() -> None:
    created = client.post(
        "/api/scheduler/tasks",
        json={
            "name": "mcp",
            "prompt": "p",
            "cron": "0 9 * * *",
            "mcp_servers": ["github", "filesystem"],
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["mcp_servers"] == ["github", "filesystem"]

    cleared = client.patch(f"/api/scheduler/tasks/{body['task_id']}", json={"mcp_servers": []})
    assert cleared.status_code == 200
    assert cleared.json()["mcp_servers"] == []


def test_invalid_schedule_model_rejected() -> None:
    missing = client.post(
        "/api/scheduler/tasks",
        json={"name": "bad", "prompt": "p", "schedule_kind": "periodic"},
    )
    assert missing.status_code == 400

    unknown = client.post(
        "/api/scheduler/tasks",
        json={
            "name": "bad",
            "prompt": "p",
            "schedule_kind": "periodic",
            "schedule_spec": {"unit": "never"},
        },
    )
    assert unknown.status_code == 400


def test_once_disables_after_run() -> None:
    store = TaskStore(":memory:")
    task = ScheduledTask(
        task_id="st-once",
        name="once",
        cron="",
        prompt="p",
        schedule_kind="once",
        schedule_spec={"date": "2026-10-30", "hour": 16, "minute": 0},
    )
    saved = store.save(task, now=datetime(2026, 10, 30, 15, 0))

    async def _runner(_task: ScheduledTask) -> None:
        return None

    scheduler = TaskScheduler(store, _runner)
    scheduler._advance(saved, datetime(2026, 10, 30, 16, 0))
    reloaded = store.get("st-once")
    assert reloaded is not None
    assert reloaded.next_run_at is None
    assert reloaded.enabled is False


def test_registry_filtered_copy_keeps_only_allowed_mcp_server() -> None:
    registry = ToolRegistry()
    registry._tools = {
        "read": SimpleNamespace(name="read", mcp_remote=False, provenance=""),
        "github_issue": SimpleNamespace(
            name="github_issue", mcp_remote=True, provenance="mcp:github"
        ),
        "fs_read": SimpleNamespace(name="fs_read", mcp_remote=True, provenance="mcp:fs"),
        "mcp_list_resources": SimpleNamespace(
            name="mcp_list_resources", mcp_remote=False, provenance=""
        ),
    }
    registry._prefix_members = {"github": {"github_issue"}, "fs": {"fs_read"}}
    kept = _filter_mcp_tools(registry, ["github"])
    assert set(kept._tools) == {"read", "github_issue"}
    assert kept._prefix_members == {"github": {"github_issue"}}
