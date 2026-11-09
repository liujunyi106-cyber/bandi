# 功能：preflight API 契约——GET /api/company/external/preflight 暴露外部引擎探测

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from bandi.api.app import app

_EXPECTED_AGENT_FIELDS = {
    "agent",
    "enabled",
    "command",
    "available",
    "binary",
    "version",
    "launch_command",
    "stdin_policy",
    "collaboration_rpc_transport",
    "isolated_home",
    "issues",
    "warnings",
    "write_checks",
    "ok",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 隔离真实 HOME：preflight 的 workspace/写权限探针全部落在 tmp_path
    import bandi.company.preflight as preflight_mod

    monkeypatch.setattr(preflight_mod, "_get_oponica_home", lambda: tmp_path)
    # 与 test_api.py 同一认证机制：patch 中间件 API key（环境未设 BANDI_API_KEY）。
    # 注意 bandi.api.__init__ 导出的 FastAPI 实例遮蔽了模块名，须从 sys.modules 取
    import sys

    app_module = sys.modules["bandi.api.app"]

    monkeypatch.setattr(app_module, "_API_KEY", "test")
    client = TestClient(app)
    client.headers["Authorization"] = "Bearer test"
    return client


def test_preflight_endpoint_reports_four_agents(client):
    resp = client.get(
        "/api/company/external/preflight",
        params={"probe_commands": False, "prepare_surfaces": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) >= {"ok", "agents", "available_agents"}
    assert {a["agent"] for a in data["agents"]} == {"codex", "claude_code", "cursor", "opencode"}
    for a in data["agents"]:
        assert _EXPECTED_AGENT_FIELDS <= set(a)


def test_preflight_endpoint_flags_missing_binary_as_unavailable(client):
    resp = client.get(
        "/api/company/external/preflight",
        params={"probe_commands": False, "prepare_surfaces": False},
    )
    data = resp.json()
    for a in data["agents"]:
        if not a["available"]:
            assert a["ok"] is False
            assert a["issues"], "不可用引擎必须携带 issues 说明"


def test_preflight_endpoint_does_not_500_on_missing_home(client):
    # 真实环境无 ~/.bandi 也能 200（写探针自建目录），不得 500
    resp = client.get(
        "/api/company/external/preflight",
        params={"probe_commands": False, "prepare_surfaces": True},
    )
    assert resp.status_code == 200
