"""契约：本地服务的浏览器来源基线（CSRF / DNS rebinding 护栏）。

写请求只接受本机回环 Origin；`Sec-Fetch-Site: cross-site` 一律 403；
Origin 缺失时看 Referer；未配置 API key 且带 Origin 的请求校验 Host 必须是回环。
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

_PROBE = "/api/__origin_guard_probe__"


def _module() -> object:
    return importlib.import_module("bandi.api.app")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    module = _module()
    monkeypatch.setattr(module, "_API_KEY", "guard-key", raising=False)
    return TestClient(module.app, headers={"Authorization": "Bearer guard-key"})


def _assert_forbidden(response) -> None:
    assert response.status_code == 403, f"应被来源基线拦截，实际 {response.status_code}"
    assert response.json()["code"] == "PERMISSION_DENIED"


def test_write_from_non_loopback_origin_is_blocked(client: TestClient) -> None:
    _assert_forbidden(client.post(_PROBE, headers={"Origin": "http://evil.example"}, json={}))


def test_write_from_loopback_origin_passes_guard(client: TestClient) -> None:
    # 未知路径 → 404 说明已越过来源基线（未被 403 拦）
    response = client.post(_PROBE, headers={"Origin": "http://localhost:5173"}, json={})
    assert response.status_code == 404


def test_cross_site_fetch_site_is_blocked(client: TestClient) -> None:
    _assert_forbidden(
        client.post(
            _PROBE,
            headers={"Origin": "http://localhost:5173", "Sec-Fetch-Site": "cross-site"},
            json={},
        )
    )


def test_read_requests_are_not_origin_gated(client: TestClient) -> None:
    assert client.get(_PROBE, headers={"Origin": "http://evil.example"}).status_code == 404


def test_get_rejects_spoofed_host_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_API_KEY", "", raising=False)
    loopback_client = TestClient(module.app, client=("127.0.0.1", 50124))
    response = loopback_client.get(_PROBE, headers={"Host": "evil.example"})
    _assert_forbidden(response)


def test_websocket_rejects_non_loopback_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.websockets import WebSocketDisconnect

    module = _module()
    monkeypatch.setattr(module, "_API_KEY", "", raising=False)
    loopback_client = TestClient(module.app, client=("127.0.0.1", 50125))
    with pytest.raises(WebSocketDisconnect):
        with loopback_client.websocket_connect(
            "/api/agent/stream",
            headers={"Origin": "http://evil.example", "Host": "127.0.0.1:50125"},
        ) as websocket:
            websocket.receive_text()


def test_websocket_rejects_null_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.websockets import WebSocketDisconnect

    module = _module()
    monkeypatch.setattr(module, "_API_KEY", "", raising=False)
    loopback_client = TestClient(module.app, client=("127.0.0.1", 50126))
    with pytest.raises(WebSocketDisconnect):
        with loopback_client.websocket_connect(
            "/api/agent/stream",
            headers={"Origin": "null", "Host": "127.0.0.1:50126"},
        ) as websocket:
            websocket.receive_text()


def test_referer_checked_when_origin_absent(client: TestClient) -> None:
    _assert_forbidden(client.post(_PROBE, headers={"Referer": "http://evil.example/x"}, json={}))


def test_null_origin_is_rejected(client: TestClient) -> None:
    # 破坏者：沙箱 iframe / file:// 页面的 Origin 是 "null"——不得借此绕过来源基线。
    # 桌面壳必须由 FastAPI 同源托管页面；改自定义协议时需显式登记允许来源。
    response = client.post(
        _PROBE,
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        json={},
    )
    _assert_forbidden(response)


def test_host_must_be_loopback_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    monkeypatch.setattr(module, "_API_KEY", "", raising=False)
    loopback_client = TestClient(module.app, client=("127.0.0.1", 50123))

    spoofed = loopback_client.post(
        _PROBE,
        headers={"Origin": "http://localhost:5173", "Host": "evil.example"},
        json={},
    )
    _assert_forbidden(spoofed)

    normal = loopback_client.post(
        _PROBE,
        headers={"Origin": "http://localhost:5173", "Host": "127.0.0.1:50123"},
        json={},
    )
    assert normal.status_code == 404
