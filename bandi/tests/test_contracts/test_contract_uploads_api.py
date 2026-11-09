# 功能：契约——附件上传端点（api/company/uploads：multipart 落盘 + 读回引用）。
# 钉行为不钉实现：上传返回引用 id / 读回字节逐字（中文）/ 项目上下文隔离（p-alpha 与
# company 互不可见）/ 破坏者：文件名穿越零逃逸 / 超大 fail-closed（413）/ 未知 id 404 /
# 项目不存在 404 / 非法 project_id 400 / DoD-12 端到端（两项目同名文件不互串）。
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from bandi.api.app import app

client = TestClient(app)
client.headers["Authorization"] = "Bearer test"


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """company 装配 + 项目库/附件库隔离：Path.home→tmp + 全局单例重置。"""
    from bandi.core.config import BandiConfig

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = BandiConfig()
    cfg.llm.api_key = ""
    with (
        patch("bandi.core.config.BandiConfig.load", return_value=cfg),
        patch("bandi.api.app._API_KEY", "test"),
        patch("bandi.api.app._company_dispatcher", None),
        patch("bandi.api.app._custom_runtime_runner", None),
        patch("bandi.api.app._team_store", None),
        patch("bandi.api.app._project_store", None),
        patch("bandi.api.app._attachment_store", None),
        patch("bandi.api.app._db_path", tmp_path / ".bandi" / "data" / "bandi.db"),
        patch("bandi.api.app._store", None),
    ):
        yield


def _create_project(project_id: str = "p-alpha") -> None:
    resp = client.post("/api/company/projects", json={"project_id": project_id, "name": project_id})
    assert resp.status_code == 200, resp.text


def _upload(payload: bytes, filename: str, project_id: str = "") -> dict:
    params = {"project_id": project_id} if project_id else {}
    resp = client.post(
        "/api/company/uploads",
        params=params,
        files={"file": (filename, payload, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["upload"]


class TestUploadApi:
    def test_upload_and_readback_roundtrip(self) -> None:
        payload = "选品计划：中文内容 + 换行\n第二行".encode()
        upload = _upload(payload, "选品计划.txt")
        assert upload["upload_id"]
        assert upload["size"] == len(payload)
        assert upload["name"].endswith(".txt")

        resp = client.get(f"/api/company/uploads/{upload['upload_id']}")
        assert resp.status_code == 200
        assert resp.content == payload, "读回字节必须逐字一致"
        disposition = unquote(resp.headers.get("content-disposition", ""))
        assert "选品计划" in disposition, f"读回必须带原始文件名，实际 {disposition}"

    def test_project_scope_isolation(self, tmp_path: Path) -> None:
        _create_project("p-alpha")
        upload = _upload("项目附件".encode(), "a.txt", project_id="p-alpha")
        stored = tmp_path / ".bandi" / "company" / "projects" / "p-alpha" / "uploads"
        assert stored.is_dir() and any(stored.glob(f"{upload['upload_id']}__*")), (
            "附件必须落项目附件目录"
        )

        resp = client.get(
            f"/api/company/uploads/{upload['upload_id']}", params={"project_id": "p-alpha"}
        )
        assert resp.status_code == 200
        resp_company = client.get(f"/api/company/uploads/{upload['upload_id']}")
        assert resp_company.status_code == 404, "项目附件不得从 company 空间读到（隔离）"

    def test_breaker_filename_traversal_zero_escape(self, tmp_path: Path) -> None:
        _create_project("p-alpha")
        upload = _upload(b"evil", "../../evil.txt", project_id="p-alpha")
        assert (
            ".." not in upload["name"] and "/" not in upload["name"] and "\\" not in upload["name"]
        )
        uploads_dir = tmp_path / ".bandi" / "company" / "projects" / "p-alpha" / "uploads"
        assert all(p.parent == uploads_dir for p in uploads_dir.glob("*")), "落盘不得逃出附件目录"
        assert not (tmp_path / ".bandi" / "company" / "evil.txt").exists()
        assert not (tmp_path / "evil.txt").exists()

    def test_breaker_oversize_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bandi.company.attachments.MAX_UPLOAD_BYTES", 1024)
        resp = client.post(
            "/api/company/uploads",
            files={"file": ("big.bin", b"x" * 2048, "application/octet-stream")},
        )
        assert resp.status_code == 413, "超限上传必须 fail-closed"

    def test_unknown_upload_404(self) -> None:
        resp = client.get("/api/company/uploads/deadbeef0000")
        assert resp.status_code == 404

    def test_unknown_project_404(self) -> None:
        resp = client.post(
            "/api/company/uploads",
            params={"project_id": "ghost-project"},
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 404
        resp2 = client.get(
            "/api/company/uploads/deadbeef0000", params={"project_id": "ghost-project"}
        )
        assert resp2.status_code == 404

    def test_breaker_invalid_project_id_400(self) -> None:
        resp = client.post(
            "/api/company/uploads",
            params={"project_id": "a b"},
            files={"file": ("a.txt", b"x", "text/plain")},
        )
        assert resp.status_code == 400, "非法 project_id 必须 400 不 500"

    def test_breaker_invalid_upload_id_400(self) -> None:
        resp = client.get("/api/company/uploads/a b")
        assert resp.status_code == 400, "非法 upload_id 必须 400 不 500"


class TestAttachmentStoreUnit:
    """store 级：落盘/读回/超限/穿越（模块行为最小覆盖）。"""

    def _store(self, tmp_path: Path) -> Any:
        from bandi.company.attachments import AttachmentStore

        return AttachmentStore(base_dir=str(tmp_path / "home"))

    def test_save_read_roundtrip(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        record = store.save("内容".encode(), filename="说明 文档.md", project_id="company")
        assert record.size == len("内容".encode())
        assert store.read(record.upload_id) == "内容".encode()
        assert store.display_name(Path(record.path)) == "说明_文档.md"

    def test_save_oversize_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from bandi.company.attachments import AttachmentTooLargeError

        monkeypatch.setattr("bandi.company.attachments.MAX_UPLOAD_BYTES", 4)
        store = self._store(tmp_path)
        with pytest.raises(AttachmentTooLargeError):
            store.save(b"12345", filename="x.bin")
        assert not any((tmp_path / "home").rglob("*"))

    def test_resolve_traversal_rejected(self, tmp_path: Path) -> None:
        from bandi.company.attachments import AttachmentError

        store = self._store(tmp_path)
        with pytest.raises(AttachmentError):
            store.resolve("../evil", project_id="p-alpha")
        with pytest.raises(AttachmentError):
            store.uploads_dir("a b")


class TestUploadDod12:
    def test_same_name_files_do_not_cross_projects(self) -> None:
        """DoD-12：两项目同名文件互不干扰，各自读回各自内容。"""
        _create_project("p-alpha")
        _create_project("p-beta")
        first = _upload("甲方内容".encode(), "同名.txt", project_id="p-alpha")
        second = _upload("乙方内容".encode(), "同名.txt", project_id="p-beta")
        assert first["upload_id"] != second["upload_id"]

        resp_a = client.get(
            f"/api/company/uploads/{first['upload_id']}", params={"project_id": "p-alpha"}
        )
        resp_b = client.get(
            f"/api/company/uploads/{second['upload_id']}", params={"project_id": "p-beta"}
        )
        assert resp_a.content == "甲方内容".encode()
        assert resp_b.content == "乙方内容".encode()
        crossed = client.get(
            f"/api/company/uploads/{first['upload_id']}", params={"project_id": "p-beta"}
        )
        assert crossed.status_code == 404, "跨项目引用必须不可见"
