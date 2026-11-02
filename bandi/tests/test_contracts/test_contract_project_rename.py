"""契约：项目改名（PATCH /api/company/projects/{project_id}）。

承诺行为：仅改 name、project_id 不可变、缺失 404、空名 400、
重复名允许（项目间不同 id）、其他字段（root_dir/team_ids）不被改动。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app
from bandi.company.projects import ProjectStore

_APP_MODULE = sys.modules["bandi.api.app"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    # 项目 store 是模块级单例——不重置会沿用别的用例建的实例
    monkeypatch.setattr(_APP_MODULE, "_project_store", None, raising=False)
    # 鉴权中间件读模块级 _API_KEY：全量下别的用例泄漏 api_key 时会 401，
    # 本组用例显式钉住「未配 key」分支（回落到 host 校验），不依赖环境状态
    monkeypatch.setattr(_APP_MODULE, "_API_KEY", "", raising=False)


@pytest.fixture()
def client() -> TestClient:
    test_client = TestClient(app, client=("127.0.0.1", 50000))
    return test_client


def _store(tmp_path: Path) -> ProjectStore:
    return _APP_MODULE._get_project_store()  # type: ignore[attr-defined]


def _make(store: ProjectStore, pid: str, name: str, root_dir: str = "") -> None:
    store.create_project(pid, name, root_dir)


def test_patch_renames_only_name(client: TestClient, tmp_path: Path) -> None:
    # 功能：改名成功且只影响 name
    store = _store(tmp_path)
    _make(store, "p1", "旧名字", root_dir="C:/work")
    store.save_project(store.get_project("p1").model_copy(update={"team_ids": ["t1"]}))

    resp = client.patch("/api/company/projects/p1", json={"name": "新名字"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project"]["name"] == "新名字"
    saved = store.get_project("p1")
    assert saved is not None
    assert saved.name == "新名字"
    assert saved.project_id == "p1"
    assert saved.root_dir == "C:/work", "root_dir 不得被改名牵连"
    assert saved.team_ids == ["t1"], "team_ids 不得被改名牵连"


def test_patch_blank_name_rejected(client: TestClient, tmp_path: Path) -> None:
    # 功能：空名与纯空白名一律拒绝，且不落盘
    store = _store(tmp_path)
    _make(store, "p2", "原名")

    for bad in ("", "   "):
        resp = client.patch("/api/company/projects/p2", json={"name": bad})
        assert resp.status_code == 400, f"name={bad!r} 应被拒：{resp.text}"
        assert resp.json()["code"] == "VALIDATION_ERROR"

    assert store.get_project("p2").name == "原名"


def test_patch_missing_project_404(client: TestClient) -> None:
    # 功能：项目不存在 → 404 且错误码专一
    resp = client.patch("/api/company/projects/ghost", json={"name": "x"})

    assert resp.status_code == 404, resp.text
    assert resp.json()["message"] == "PROJECT_NOT_FOUND"


def test_patch_cannot_change_project_id(client: TestClient, tmp_path: Path) -> None:
    # 功能：project_id 不可通过改名变更（路径为准，请求体里的 project_id 被忽略）
    store = _store(tmp_path)
    _make(store, "p3", "原名")

    resp = client.patch("/api/company/projects/p3", json={"name": "新名", "project_id": "hijack"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["project"]["project_id"] == "p3"
    assert store.get_project("hijack") is None, "不得创建出新的 project_id"


def test_patch_duplicate_name_allowed(client: TestClient, tmp_path: Path) -> None:
    # 功能：不同 id 可同名（项目名不要求唯一，避免误加唯一约束）
    store = _store(tmp_path)
    _make(store, "a1", "重名")
    _make(store, "a2", "别 名")

    resp = client.patch("/api/company/projects/a2", json={"name": "重名"})

    assert resp.status_code == 200, resp.text
    assert store.get_project("a2").name == "重名"


def test_patch_unknown_field_ignored(client: TestClient, tmp_path: Path) -> None:
    # 功能：只接受 name，未知字段不报错也不生效（宽容读、严格写）
    store = _store(tmp_path)
    _make(store, "p4", "原名")

    resp = client.patch("/api/company/projects/p4", json={"name": "改名了", "root_dir": "C:/evil"})

    assert resp.status_code == 200, resp.text
    saved = store.get_project("p4")
    assert saved.name == "改名了"
    assert saved.root_dir == "", "root_dir 不得被未知字段改写"


def test_store_rename_updates_timestamp(client: TestClient, tmp_path: Path) -> None:
    # 功能：改名后 updated_at 必须推进（前端按此判断缓存失效）
    store = _store(tmp_path)
    _make(store, "p5", "原名")
    before = store.get_project("p5").updated_at

    client.patch("/api/company/projects/p5", json={"name": "改名后"})

    after = store.get_project("p5").updated_at
    assert after > before, f"updated_at 未推进：{before} -> {after}"
