# 功能：账号 API 端点契约（FE-装配面）
# + GET /{user_id}），生产装配 ~/.bandi/data/account.db + 惰性 ensure_local_user。
# 契约钉：建用户→读回（tenant 默认/角色 member）、未知 404、非法角色 400、local 默认账号存在。
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with (
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._account_store", None),
    ):
        yield


class TestAccountUsersApi:
    def test_local_user_present(self) -> None:
        resp = client.get("/api/account/users")
        assert resp.status_code == 200
        users = resp.json()["users"]
        local = [u for u in users if u["user_id"] == "local"]
        assert local, "单机默认账号 local 必须存在（ensure_local_user 惰性装配）"
        assert local[0]["tenant_id"] == "default"

    def test_create_and_read_back(self) -> None:
        resp = client.post("/api/account/users", json={"user_id": "alice", "display_name": "Alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "alice"
        assert data["tenant_id"] == "default"
        assert "member" in data["roles"]

        resp = client.get("/api/account/users/alice")
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Alice"

    def test_create_with_tenant_and_roles(self) -> None:
        resp = client.post(
            "/api/account/users",
            json={
                "user_id": "bob",
                "display_name": "Bob",
                "tenant_id": "acme",
                "roles": ["admin", "member"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_id"] == "acme"
        assert set(data["roles"]) == {"admin", "member"}

    def test_create_missing_fields_400(self) -> None:
        resp = client.post("/api/account/users", json={"user_id": "x"})
        assert resp.status_code == 400

    def test_create_invalid_role_400(self) -> None:
        resp = client.post(
            "/api/account/users",
            json={"user_id": "y", "display_name": "Y", "roles": ["superadmin"]},
        )
        assert resp.status_code == 400

    def test_get_missing_404(self) -> None:
        resp = client.get("/api/account/users/nobody")
        assert resp.status_code == 404
