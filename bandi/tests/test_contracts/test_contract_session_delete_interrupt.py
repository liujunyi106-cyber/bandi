# 功能：契约——会话删除联动中止该会话发起的工作项
# 员工执行残留显示在新会话）。钉行为：origin_session_id 匹配项调 interrupt / 不匹配
# 不动 / 中断抛错不阻断删除 / 会话不存在 404 且零中断 / 破坏者：工作项扫描抛错删除
# 仍成功（联动是 best-effort，不得反向阻塞删除主链）。
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


def _item(item_id: str, origin_session_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=item_id, metadata={"origin_session_id": origin_session_id})


class _FakeDispatcher:
    def __init__(self, items: list[Any]) -> None:
        self.store = SimpleNamespace(list_all=lambda: list(items))
        self.interrupt_work_item = AsyncMock(return_value=True)


class _CancelDispatcher(_FakeDispatcher):
    def __init__(self, items: list[Any]) -> None:
        super().__init__(items)
        self.cancel_work_item = AsyncMock(return_value=True)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _make_session(tmp_path: Path) -> str:
    from bandi.memory.session.store import SessionStore

    db_dir = tmp_path / ".bandi" / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(str(db_dir / "bandi.db"))
    return store.create_session(title="联动词测试")


class TestDeleteSessionCancelsPending:
    def test_cancel_work_item_used_for_matching_origin(self, tmp_path: Path) -> None:
        session_id = _make_session(tmp_path)
        dispatcher = _CancelDispatcher([_item("wi-1", session_id), _item("wi-2", "other")])
        with patch("bandi.api.app._get_company_dispatcher", return_value=dispatcher):
            resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200, resp.text
        dispatcher.cancel_work_item.assert_awaited_once_with("wi-1", reason="session_deleted")
        dispatcher.interrupt_work_item.assert_not_awaited()


class TestDeleteSessionInterruptsWorkItems:
    def test_matching_origin_session_interrupted_only(self, tmp_path: Path) -> None:
        session_id = _make_session(tmp_path)
        dispatcher = _FakeDispatcher([_item("wi-1", session_id), _item("wi-2", "other-session")])
        with patch("bandi.api.app._get_company_dispatcher", return_value=dispatcher):
            resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        dispatcher.interrupt_work_item.assert_awaited_once_with("wi-1")

    def test_interrupt_failure_does_not_block_delete(self, tmp_path: Path) -> None:
        session_id = _make_session(tmp_path)
        dispatcher = _FakeDispatcher([_item("wi-1", session_id)])
        dispatcher.interrupt_work_item = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("bandi.api.app._get_company_dispatcher", return_value=dispatcher):
            resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200, "中断失败不得反向阻塞会话删除"

    def test_scan_failure_does_not_block_delete(self, tmp_path: Path) -> None:
        session_id = _make_session(tmp_path)
        dispatcher = _FakeDispatcher([])
        dispatcher.store = SimpleNamespace(list_all=_raise_scan)
        with patch("bandi.api.app._get_company_dispatcher", return_value=dispatcher):
            resp = client.delete(f"/api/sessions/{session_id}")
        assert resp.status_code == 200, "工作项扫描失败不得反向阻塞会话删除"

    def test_missing_session_404_and_zero_interrupt(self, tmp_path: Path) -> None:
        dispatcher = _FakeDispatcher([_item("wi-1", "absent-session")])
        with patch("bandi.api.app._get_company_dispatcher", return_value=dispatcher):
            resp = client.delete("/api/sessions/absent-session")
        assert resp.status_code == 404
        dispatcher.interrupt_work_item.assert_not_awaited()


def _raise_scan() -> list[Any]:
    raise RuntimeError("scan boom")
