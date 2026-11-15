# 功能：契约——群聊回合 API（查询/恢复/取消）与调度器中止接线。
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

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
    def __init__(self) -> None:
        self.round = SimpleNamespace(
            round_id="r1",
            session_id="s1",
            project_id="p1",
            status=SimpleNamespace(value="running"),
        )
        self.resumed = 0
        self.cancelled = 0

    def get_active(self, session_id: str) -> Any | None:
        return self.round if session_id == self.round.session_id else None

    def get(self, round_id: str) -> Any | None:
        return self.round if round_id == self.round.round_id else None

    def snapshot(self, round_: Any) -> dict[str, Any]:
        return {
            "round_id": round_.round_id,
            "session_id": round_.session_id,
            "project_id": round_.project_id,
            "status": "running",
            "slots": [],
        }

    def resume(self, round_id: str) -> Any:
        self.resumed += 1
        return SimpleNamespace(id="wi-next")

    def cancel(self, round_id: str, reason: str = "") -> list[str]:
        self.cancelled += 1
        return ["wi-1"]


def _dispatcher(service: _FakeService) -> SimpleNamespace:
    return SimpleNamespace(
        group_rounds=service,
        cancel_work_item=AsyncMock(return_value=True),
    )


class TestRoundApi:
    def test_get_round_by_session(self) -> None:
        service = _FakeService()
        with patch("bandi.api.app._get_company_dispatcher", return_value=_dispatcher(service)):
            resp = client.get("/api/company/rounds?session_id=s1&project_id=p1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["round"]["round_id"] == "r1"

    def test_get_round_requires_session(self) -> None:
        service = _FakeService()
        with patch("bandi.api.app._get_company_dispatcher", return_value=_dispatcher(service)):
            resp = client.get("/api/company/rounds")
        assert resp.status_code == 400

    def test_get_round_project_mismatch_returns_null(self) -> None:
        service = _FakeService()
        with patch("bandi.api.app._get_company_dispatcher", return_value=_dispatcher(service)):
            resp = client.get("/api/company/rounds?session_id=s1&project_id=other")
        assert resp.status_code == 200
        assert resp.json()["round"] is None

    def test_resume_round(self) -> None:
        service = _FakeService()
        with patch("bandi.api.app._get_company_dispatcher", return_value=_dispatcher(service)):
            resp = client.post("/api/company/rounds/r1/resume")
        assert resp.status_code == 200, resp.text
        assert resp.json()["work_item_id"] == "wi-next"
        assert service.resumed == 1

    def test_cancel_round_interrupts_work_items(self) -> None:
        service = _FakeService()
        dispatcher = _dispatcher(service)
        with patch("bandi.api.app._get_company_dispatcher", return_value=dispatcher):
            resp = client.post("/api/company/rounds/r1/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["cancelled_work_items"] == ["wi-1"]
        dispatcher.cancel_work_item.assert_awaited_once_with("wi-1", reason="user_cancel")

    def test_missing_round_404(self) -> None:
        service = _FakeService()
        with patch("bandi.api.app._get_company_dispatcher", return_value=_dispatcher(service)):
            resp = client.post("/api/company/rounds/missing/resume")
        assert resp.status_code == 404
