from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    app_module = sys.modules["bandi.api.app"]
    monkeypatch.setattr(app_module, "_API_KEY", "test")


class _FakeService:
    def __init__(self, goal: Any | None) -> None:
        self.goal = goal

    def get(self, session_id: str) -> Any | None:
        return self.goal if session_id == "s1" else None

    def snapshot(self, goal: Any) -> dict[str, Any]:
        return {
            "goal_id": goal.goal_id,
            "session_id": goal.session_id,
            "title": goal.title,
            "status": goal.status,
            "current": goal.current,
            "total": goal.total,
            "stage_title": goal.stage_title,
        }


def _dispatcher(service: Any) -> Any:
    return SimpleNamespace(group_goals=service)


def test_goal_api_returns_null_without_goal():
    with patch(
        "bandi.api.app._get_company_dispatcher", return_value=_dispatcher(_FakeService(None))
    ):
        response = client.get("/api/company/goals?session_id=s1")
    assert response.status_code == 200
    assert response.json() == {"goal": None}


def test_goal_api_returns_snapshot():
    goal = SimpleNamespace(
        goal_id="g1",
        session_id="s1",
        title="目标账本",
        status="active",
        current=2,
        total=4,
        stage_title="顶部目标条",
    )
    with patch(
        "bandi.api.app._get_company_dispatcher",
        return_value=_dispatcher(_FakeService(goal)),
    ):
        response = client.get("/api/company/goals?session_id=s1")
    assert response.status_code == 200
    payload = response.json()["goal"]
    assert payload["title"] == "目标账本"
    assert payload["current"] == 2
    assert payload["total"] == 4
