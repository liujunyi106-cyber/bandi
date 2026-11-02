# 功能：契约——设置面（系统信息/存储/权限档位/保留清理/MCP 开关）
# 钉行为：白名单外键 400、档位只动询问范围（deny 底线不可改）、检索密钥加密且不回显、
# 清理必须 confirm 且与预览同口径、MCP 停用后不注册工具（装配层由 factory 保证）。
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bandi.api import settings_ext


@pytest.fixture()
def api_key(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """功能：注入固定 API key，配合 Bearer 头调用真实端点。"""
    module = sys.modules["bandi.api.app"]
    monkeypatch.setattr(module, "_API_KEY", "test-key", raising=False)
    return {"Authorization": "Bearer test-key"}


def _fresh_store() -> Any:
    from bandi.api.app import _get_store

    api_module = sys.modules["bandi.api.app"]
    with api_module._store_lock:
        api_module._store = None
    return _get_store()


def _client() -> Any:
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    return TestClient(app)


def _config_path() -> Path:
    return Path.home() / ".bandi" / "config.yaml"


def test_preset_only_touches_ask_scope(api_key: dict[str, str]) -> None:
    with _client() as client:
        before = client.get("/api/settings/preferences", headers=api_key).json()
        deny_before = before["permission"]["deny_commands"]

        applied = client.put(
            "/api/settings/preferences",
            json={"permission_preset": "strict"},
            headers=api_key,
        ).json()
        assert applied["permission"]["ask_risk_levels"] == ["medium", "high", "critical"]
        # 破坏者：任何档位都不得放宽 deny 底线
        assert applied["permission"]["deny_commands"] == deny_before
        assert deny_before, "默认 deny 规则不得为空"

        loose = client.put(
            "/api/settings/preferences",
            json={"permission_preset": "loose"},
            headers=api_key,
        ).json()
        assert loose["permission"]["ask_risk_levels"] == ["critical"]
        assert loose["permission"]["deny_commands"] == deny_before


def test_unknown_field_and_bad_values_are_rejected(api_key: dict[str, str]) -> None:
    with _client() as client:
        unknown = client.put(
            "/api/settings/preferences", json={"budget_limit": 10}, headers=api_key
        )
        assert unknown.status_code == 400
        assert "UNKNOWN_FIELDS" in unknown.text

        bad_preset = client.put(
            "/api/settings/preferences",
            json={"permission_preset": "yolo"},
            headers=api_key,
        )
        assert bad_preset.status_code == 400

        bad_days = client.put(
            "/api/settings/preferences", json={"retention_days": 45}, headers=api_key
        )
        assert bad_days.status_code == 400


def test_web_search_key_is_encrypted_and_never_echoed(api_key: dict[str, str]) -> None:
    plaintext = "tvly-secret-value-123"
    with _client() as client:
        response = client.put(
            "/api/settings/preferences",
            json={"web_search": {"tavily_api_key": plaintext}},
            headers=api_key,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["web_search"]["tavily_configured"] is True
        # 破坏者：出参不得泄漏明文密钥
        assert plaintext not in json.dumps(body, ensure_ascii=False)

    raw = open(_config_path(), encoding="utf-8").read()
    assert plaintext not in raw, "配置文件中不得出现明文密钥"
    assert "tavily_api_key" in raw

    # 配置优先：写入后可被解析面读到（env 未设置时也生效）
    cfg = settings_ext.BandiConfig.load()
    assert settings_ext.resolve_web_search_key(cfg, "tavily_api_key") == plaintext


def test_system_info_and_storage_hide_secrets(api_key: dict[str, str]) -> None:
    with _client() as client:
        info = client.get("/api/system/info", headers=api_key).json()
        assert info["version"] and info["python"]
        assert "api_key" not in json.dumps(info)

        storage = client.get("/api/system/storage", headers=api_key).json()
        assert storage["data_dir"]
        assert set(storage["disk"]) == {"total", "used", "free"}
        assert isinstance(storage["dbs"], list)


def test_retention_preview_matches_cleanup_scope(api_key: dict[str, str]) -> None:
    store = _fresh_store()
    old = store.create_session(title="过期会话")
    fresh = store.create_session(title="新会话")
    store.update_session(old, updated_at=(datetime.now() - timedelta(days=120)).isoformat())

    with _client() as client:
        preview = client.get("/api/settings/retention/preview?days=90", headers=api_key).json()
        assert preview["sessions"] == 1
        # 破坏者：未确认不得删除
        refused = client.post("/api/settings/retention/cleanup", json={"days": 90}, headers=api_key)
        assert refused.status_code == 400
        assert store.get_session(old) is not None

        cleaned = client.post(
            "/api/settings/retention/cleanup",
            json={"days": 90, "confirm": True},
            headers=api_key,
        ).json()
        assert cleaned["deleted"] == 1
        assert store.get_session(old) is None
        assert store.get_session(fresh) is not None


def test_mcp_global_toggle_persists(api_key: dict[str, str]) -> None:
    with _client() as client:
        listed = client.get("/api/mcp/servers", headers=api_key).json()
        assert listed["global"]["max_exposed_tools"] >= 0

        ok = client.put("/api/mcp/global", json={"lazy_exposure": False}, headers=api_key)
        assert ok.status_code == 200
        after = client.get("/api/mcp/servers", headers=api_key).json()
        assert after["global"]["lazy_exposure"] is False

        bad = client.put("/api/mcp/global", json={"max_exposed_tools": 999}, headers=api_key)
        assert bad.status_code == 400
        unknown = client.put("/api/mcp/global", json={"nope": 1}, headers=api_key)
        assert unknown.status_code == 400


def test_mcp_patch_and_test_unknown_server(api_key: dict[str, str]) -> None:
    with _client() as client:
        missing = client.patch("/api/mcp/servers/no-such", json={"enabled": False}, headers=api_key)
        assert missing.status_code == 404
        tested = client.post("/api/mcp/servers/no-such/test", headers=api_key)
        assert tested.status_code == 404
        bad_field = client.patch(
            "/api/mcp/servers/no-such", json={"command": "rm"}, headers=api_key
        )
        assert bad_field.status_code == 400


def test_tools_meta_payload_shape() -> None:
    """能力管理契约：/tools/meta 是**数组**且带 risk_level / mcp（前端按此渲染，取 .tools 会空表）。"""
    from bandi.agent.factory import build_tool_registry

    metas = build_tool_registry().list_tool_meta()
    assert isinstance(metas, list) and metas, "工具目录不得为空"
    by_name = {item["name"]: item for item in metas}
    bash = by_name["bash"]
    assert bash["risk_level"] == "high", bash
    assert bash["mcp"] is False
    assert {"name", "category", "feature_group", "capability_tags"} <= set(bash)
