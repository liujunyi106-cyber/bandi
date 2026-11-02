"""契约：POST /api/fs/open —— 回复引用文件点击打开（沙箱 + 存在性 + 系统调用）。"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# bandi.api.app 模块内定义了 app = FastAPI()，属性访问拿到的是实例；
# 需要模块本体（monkeypatch _open_local_folder 等模块级符号）故走 importlib。
app_module = importlib.import_module("bandi.api.app")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离：工作区根指向 tmp；_open_local_folder 截获系统调用；鉴权注入测试 key。"""
    from bandi.tools import fs_tool

    monkeypatch.setenv("BANDI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(fs_tool, "workspace_root", lambda: tmp_path)
    opened: list[str] = []
    monkeypatch.setattr(app_module, "_open_local_folder", lambda p: opened.append(p))
    monkeypatch.setattr(app_module, "_get_project_store", lambda: _EmptyStore())
    monkeypatch.setattr(app_module, "_API_KEY", "test")
    with TestClient(app_module.app) as test_client:
        test_client.headers["Authorization"] = "Bearer test"
        test_client.opened = opened  # type: ignore[attr-defined]
    return test_client


class _EmptyStore:
    def list_projects(self):
        return []


@pytest.fixture()
def session_client(tmp_path, monkeypatch):
    """隔离：鉴权 + home 指 tmp + 隔离会话库。"""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(app_module, "_API_KEY", "test")
    monkeypatch.setattr(app_module, "_db_path", tmp_path / "bandi.db")
    monkeypatch.setattr(app_module, "_store", None)
    with TestClient(app_module.app) as test_client:
        test_client.headers["Authorization"] = "Bearer test"
    return test_client


def test_session_detail_excludes_checkpoint_entries(session_client):
    """checkpoint 是状态快照（role/content 空），不应混进 messages 返回。"""
    store = app_module._get_store()
    sid = store.create_session(title="T")
    store.append_message(
        sid,
        SimpleNamespace(
            role="user",
            content="你好",
            meta={},
            name=None,
            tool_calls=[],
            turn_index=0,
            tool_call_id=None,
        ),
    )
    store.save_checkpoint_entry(sid, 1)

    resp = session_client.get(f"/api/sessions/{sid}")

    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert any(m.get("content") == "你好" for m in messages)
    assert all(m.get("entry_type") != "checkpoint" for m in messages)


def test_open_file_inside_workspace(client, tmp_path):
    target = tmp_path / "README.md"
    target.write_text("hi", encoding="utf-8")

    resp = client.post("/api/fs/open", json={"path": str(target)})

    assert resp.status_code == 200
    assert resp.json()["opened"] == str(target.resolve())
    assert client.opened == [str(target.resolve())]


def test_open_directory_inside_workspace(client, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()

    resp = client.post("/api/fs/open", json={"path": str(folder)})

    assert resp.status_code == 200


def test_open_outside_workspace_rejected(client, tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        resp = client.post("/api/fs/open", json={"path": str(outside)})
        assert resp.status_code == 403
        assert client.opened == []
    finally:
        outside.unlink(missing_ok=True)


def test_open_path_traversal_rejected(client, tmp_path):
    escape = tmp_path / ".." / ".." / "etc" / "passwd"

    resp = client.post("/api/fs/open", json={"path": str(escape)})

    assert resp.status_code in (403, 404)
    assert client.opened == []


def test_open_empty_path_rejected(client):
    assert client.post("/api/fs/open", json={"path": ""}).status_code == 400
    assert client.post("/api/fs/open", json={}).status_code == 400


def test_open_missing_path_404(client, tmp_path):
    resp = client.post("/api/fs/open", json={"path": str(tmp_path / "nope.md")})

    assert resp.status_code == 404
    assert client.opened == []


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows 建符号链接需 SeCreateSymbolicLinkPrivilege（开发者模式/管理员）",
)
def test_open_symlink_escape_rejected(client, tmp_path):
    """符号链接解析后越界 ⇒ 仍须 403（防 link 绕过）。"""
    outside_dir = tmp_path.parent / f"outside-{os.getpid()}"
    outside_dir.mkdir(exist_ok=True)
    secret = outside_dir / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(secret)
        resp = client.post("/api/fs/open", json={"path": str(link)})
        assert resp.status_code == 403
        assert client.opened == []
    finally:
        link.unlink(missing_ok=True)
        secret.unlink(missing_ok=True)
        outside_dir.rmdir()
