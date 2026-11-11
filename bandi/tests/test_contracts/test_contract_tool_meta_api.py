# 功能：Day134a /能力管理面接线契约——GET /api/tools/meta 消费 list_tool_meta（生产调用点）
# 契约钉「行为结果/最终产物」：端点返回能力元数据、与注册表面口径同步、无凭证 401。
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


def _patch_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    app_module = sys.modules["bandi.api.app"]
    monkeypatch.setattr(app_module, "_API_KEY", "test")


def test_tools_meta_returns_capability_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/tools/meta → 200，返回能力元数据（含 capability_tags），非 OpenAI function 包装。"""
    _patch_api_key(monkeypatch)
    resp = client.get("/api/tools/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and body, "能力面必须返回非空工具清单"
    item = body[0]
    assert {"name", "description", "category", "feature_group", "capability_tags"} <= set(item)
    assert "type" not in item, "不允许返回 OpenAI function 包装"


def test_tools_meta_matches_registry_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """端点产出与生产装配 build_tool_registry().list_tool_meta() 全口径一致（接线透传不漂移）。"""
    _patch_api_key(monkeypatch)
    from bandi.agent.factory import build_tool_registry

    resp = client.get("/api/tools/meta")
    assert resp.status_code == 200
    assert resp.json() == build_tool_registry().list_tool_meta()


def test_tools_meta_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """破坏者视角：无凭证访问能力面必须 401（与全局鉴权同不变式）。"""
    _patch_api_key(monkeypatch)
    anon = TestClient(app)
    assert anon.get("/api/tools/meta").status_code == 401


def test_tools_meta_cached_no_connection_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审查修复 registry 进程级缓存——第二次请求不得再开 sqlite 连接
    （修复前每请求 build_tool_registry 泄漏 6 连接）。"""
    import sqlite3
    import sys

    app_module = sys.modules["bandi.api.app"]
    monkeypatch.setattr(app_module, "_API_KEY", "test")
    monkeypatch.setattr(app_module, "_tools_meta_registry", None)

    original = sqlite3.connect
    calls: dict[str, int] = {"n": 0}

    def spy(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    first = client.get("/api/tools/meta")
    assert first.status_code == 200
    first_calls = calls["n"]
    assert first_calls > 0, "首次请求构建 registry 需要 sqlite 连接"

    calls["n"] = 0
    second = client.get("/api/tools/meta")
    assert second.status_code == 200
    assert second.json() == first.json(), "缓存实例产出一致"
    assert calls["n"] == 0, "缓存后二次请求不得新建 sqlite 连接"
