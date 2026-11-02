# 功能：契约——签名令牌 TTL（签发/校验/过期/篡改）+ payload 严格性 + WS 鉴权 + 密文 API key
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bandi.core.security.tokens import (
    MAX_TTL_SECONDS,
    issue_token,
    resolve_token_secret,
    verify_token,
)

_WIN = sys.platform == "win32"


class TestResolveSecret:
    def test_env_secret_takes_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "direct-secret")
        monkeypatch.setenv("BANDI_API_KEY", "api-key")
        assert resolve_token_secret() == "direct-secret"

    def test_derived_from_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BANDI_AUTH_SECRET", raising=False)
        monkeypatch.setenv("BANDI_API_KEY", "api-key")
        expected = hashlib.sha256(b"bandi-token:api-key").hexdigest()
        assert resolve_token_secret() == expected

    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_derived_from_protected_api_key_uses_plaintext(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 破坏者：env 存 dpapi: 密文时若直接拿密文派生，签名令牌通道与静态 key 通道
        from bandi.core.security.secret_protect import protect

        monkeypatch.delenv("BANDI_AUTH_SECRET", raising=False)
        monkeypatch.setenv("BANDI_API_KEY", protect("api-key-plain"))
        expected = hashlib.sha256(b"bandi-token:api-key-plain").hexdigest()
        assert resolve_token_secret() == expected

    def test_disabled_when_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BANDI_AUTH_SECRET", raising=False)
        monkeypatch.delenv("BANDI_API_KEY", raising=False)
        assert resolve_token_secret() == ""


class TestIssueVerify:
    def test_issue_then_verify_roundtrip(self) -> None:
        token = issue_token("alice", ttl_seconds=60, secret="s3cr3t")
        assert token.startswith("v1.")
        payload = verify_token(token, secret="s3cr3t")
        assert payload is not None
        assert payload["sub"] == "alice"
        assert payload["exp"] > payload["iat"]
        assert payload["nonce"]

    def test_ttl_enforced_by_exp(self) -> None:
        now = 1_000_000.0
        token = issue_token("a", ttl_seconds=60, secret="s", now=now)
        assert verify_token(token, secret="s", now=now + 59) is not None
        assert verify_token(token, secret="s", now=now + 61) is None

    def test_ttl_clamped_to_max(self) -> None:
        now = 1_000_000.0
        token = issue_token("a", ttl_seconds=10**9, secret="s", now=now)
        payload = verify_token(token, secret="s", now=now)
        assert payload is not None
        assert payload["exp"] - payload["iat"] == MAX_TTL_SECONDS

    def test_tampered_payload_rejected(self) -> None:
        token = issue_token("alice", ttl_seconds=60, secret="s")
        version, payload_b64, signature = token.split(".")
        forged_b64 = payload_b64[:-2] + ("AA" if not payload_b64.endswith("AA") else "BB")
        forged = f"{version}.{forged_b64}.{signature}"
        assert verify_token(forged, secret="s") is None

    def test_wrong_secret_rejected(self) -> None:
        token = issue_token("alice", ttl_seconds=60, secret="right")
        assert verify_token(token, secret="wrong") is None

    def test_garbage_token_rejected(self) -> None:
        assert verify_token("not-a-token", secret="s") is None
        assert verify_token("v1.@@@.@@@", secret="s") is None
        assert verify_token("v2.QUJD.QUJD", secret="s") is None
        assert verify_token("v1.QUJD.中文签名", secret="s") is None

    def test_empty_secret_disables_signing(self) -> None:
        token = issue_token("alice", ttl_seconds=60, secret="s")
        assert verify_token(token, secret="") is None
        with pytest.raises(ValueError):
            issue_token("alice", ttl_seconds=60, secret="")


def _forge_token(payload: dict[str, Any], secret: str) -> str:
    # 功能：用真实 伪造合法签名但 payload 不合规的令牌（校验输入边界）
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signing_input = f"v1.{payload_b64}"
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{signing_input}.{signature}"


class TestStrictPayloadValidation:
    def _payload(self) -> dict[str, Any]:
        now = int(time.time())
        return {"sub": "alice", "iat": now - 60, "exp": now + 3600, "nonce": "abcd1234"}

    def test_missing_nonce_rejected(self) -> None:
        payload = self._payload()
        payload.pop("nonce")
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_missing_sub_rejected(self) -> None:
        payload = self._payload()
        payload.pop("sub")
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_empty_sub_rejected(self) -> None:
        payload = self._payload()
        payload["sub"] = ""
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_nan_exp_rejected(self) -> None:
        # 破坏者：NaN 让 `current >= exp` 恒 False，永不过期
        payload = self._payload()
        payload["exp"] = float("nan")
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_infinite_exp_rejected(self) -> None:
        payload = self._payload()
        payload["exp"] = float("inf")
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_nan_iat_rejected(self) -> None:
        payload = self._payload()
        payload["iat"] = float("nan")
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_missing_exp_rejected(self) -> None:
        payload = self._payload()
        payload.pop("exp")
        assert verify_token(_forge_token(payload, "s"), secret="s") is None

    def test_wellformed_payload_still_accepted(self) -> None:
        token = issue_token("alice", ttl_seconds=60, secret="s")
        assert verify_token(token, secret="s") is not None


def _make_auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    # 功能：构造已鉴权 TestClient（隔离 home/db/store/config，不碰真实用户目录）
    import importlib

    from fastapi.testclient import TestClient

    from bandi.core.config import BandiConfig
    from bandi.memory.session.store import SessionStore

    app_mod = importlib.import_module("bandi.api.app")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / ".bandi" / "data" / "bandi.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    SessionStore(str(db_path)).close()
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    monkeypatch.setattr(BandiConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(app_mod, "_API_KEY", "static-key")
    monkeypatch.setattr(app_mod, "_db_path", db_path)
    monkeypatch.setattr(app_mod, "_store", None)
    client = TestClient(app_mod.app)
    client.headers["Authorization"] = "Bearer static-key"
    return client


@pytest.fixture
def auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("BANDI_AUTH_SECRET", raising=False)
    monkeypatch.delenv("BANDI_API_KEY", raising=False)
    return _make_auth_client(tmp_path, monkeypatch)


class TestApiAuthToken:
    def test_static_key_still_works(self, auth_client: Any) -> None:
        assert auth_client.get("/api/sessions").status_code == 200

    def test_wrong_static_key_rejected(self, auth_client: Any) -> None:
        resp = auth_client.get("/api/sessions", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "UNAUTHORIZED"

    def test_issue_token_and_use_for_protected_endpoint(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "test-secret")
        resp = auth_client.post("/api/auth/token", json={"subject": "tester", "ttl_seconds": 120})
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"].startswith("v1.")
        assert data["expires_at"]
        payload = verify_token(data["token"], secret="test-secret")
        assert payload is not None
        assert payload["sub"] == "tester"
        assert payload["exp"] - payload["iat"] == 120
        signed = auth_client.get(
            "/api/sessions", headers={"Authorization": f"Bearer {data['token']}"}
        )
        assert signed.status_code == 200

    def test_default_ttl_is_one_hour(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "test-secret")
        resp = auth_client.post("/api/auth/token", json={})
        assert resp.status_code == 200
        payload = verify_token(resp.json()["token"], secret="test-secret")
        assert payload is not None
        assert payload["exp"] - payload["iat"] == 3600

    def test_ttl_over_max_rejected(self, auth_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "test-secret")
        resp = auth_client.post("/api/auth/token", json={"ttl_seconds": MAX_TTL_SECONDS + 1})
        assert resp.status_code == 422

    def test_expired_signed_token_401(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "test-secret")
        stale = issue_token(
            "tester", ttl_seconds=60, secret="test-secret", now=time.time() - 100_000
        )
        resp = auth_client.get("/api/sessions", headers={"Authorization": f"Bearer {stale}"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "UNAUTHORIZED"

    def test_tampered_signed_token_401(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "test-secret")
        token = issue_token("tester", ttl_seconds=120, secret="test-secret")
        version, payload_b64, signature = token.split(".")
        forged = f"{version}.{payload_b64}.{'0' * len(signature)}"
        resp = auth_client.get("/api/sessions", headers={"Authorization": f"Bearer {forged}"})
        assert resp.status_code == 401
        assert resp.json()["code"] == "UNAUTHORIZED"

    def test_token_endpoint_503_when_signing_disabled(self, auth_client: Any) -> None:
        resp = auth_client.post("/api/auth/token", json={})
        assert resp.status_code == 503
        assert resp.json()["code"] == "SERVICE_UNAVAILABLE"


class TestApiKeyResolution:
    @pytest.mark.skipif(not _WIN, reason="DPAPI 仅在 Windows 可用")
    def test_protected_env_resolved_to_plaintext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 破坏者：_API_KEY 直接用密文原文时，持有明文 key 的客户端全部 401
        import importlib

        from bandi.core.security.secret_protect import protect

        app_mod = importlib.import_module("bandi.api.app")
        protected = protect("sk-live-secret")
        monkeypatch.setenv("BANDI_API_KEY", protected)
        assert app_mod._resolve_api_key() == "sk-live-secret"
        monkeypatch.setattr(app_mod, "_API_KEY", app_mod._resolve_api_key())
        assert app_mod._bearer_token_ok("sk-live-secret") is True
        assert app_mod._bearer_token_ok(protected) is False
        assert app_mod._bearer_token_ok(protect("sk-live-secret")) is False

    def test_plain_env_resolved_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        app_mod = importlib.import_module("bandi.api.app")
        monkeypatch.setenv("BANDI_API_KEY", "sk-plain-http")
        assert app_mod._resolve_api_key() == "sk-plain-http"


class TestWebSocketAuth:
    def test_static_bearer_still_accepted(self, auth_client: Any) -> None:
        with auth_client.websocket_connect(
            "/api/agent/stream", headers={"Authorization": "Bearer static-key"}
        ):
            pass

    def test_signed_token_via_query_accepted(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 破坏者：HTTP 通道收签名令牌而 WS 通道不收 = 同一凭证两套口径；
        # 浏览器 WebSocket 无法自带 Authorization 头，只能走 ?token=
        monkeypatch.setenv("BANDI_AUTH_SECRET", "ws-secret")
        token = issue_token("panel", ttl_seconds=120, secret="ws-secret")
        del auth_client.headers["Authorization"]
        with auth_client.websocket_connect(f"/api/agent/stream?token={token}"):
            pass

    def test_expired_signed_token_closed(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import WebSocketDisconnect

        monkeypatch.setenv("BANDI_AUTH_SECRET", "ws-secret")
        stale = issue_token("panel", ttl_seconds=60, secret="ws-secret", now=time.time() - 100_000)
        del auth_client.headers["Authorization"]
        with pytest.raises(WebSocketDisconnect):
            with auth_client.websocket_connect(f"/api/agent/stream?token={stale}"):
                pass

    def test_garbage_token_closed(self, auth_client: Any) -> None:
        from fastapi import WebSocketDisconnect

        del auth_client.headers["Authorization"]
        with pytest.raises(WebSocketDisconnect):
            with auth_client.websocket_connect("/api/agent/stream?token=garbage"):
                pass

    def test_company_ws_signed_token_via_query_accepted(
        self, auth_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BANDI_AUTH_SECRET", "ws-secret")
        token = issue_token("panel", ttl_seconds=120, secret="ws-secret")
        del auth_client.headers["Authorization"]
        with auth_client.websocket_connect(f"/ws/company?token={token}"):
            pass
