# 功能：资料库节点 API 契约——树/上传/内容读写冲突/移动成环/级联删除/预览降级/下载。

from __future__ import annotations

import io
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import bandi.api.knowledge_nodes_api as api_mod
from bandi.knowledge.files import PreviewService
from bandi.knowledge.library import KnowledgeLibraryService
from bandi.knowledge.nodes import KnowledgeNodeStore
from bandi.knowledge.storage import NodeFileStore
from bandi.knowledge.store import KnowledgeStore

ORIGIN = {"Origin": "http://127.0.0.1"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    nodes = KnowledgeNodeStore(tmp_path / "k.db")
    documents = KnowledgeStore(tmp_path / "k.db")
    files = NodeFileStore(tmp_path / "files")
    preview = PreviewService(tmp_path / "cache", soffice_path="")
    library = KnowledgeLibraryService(nodes, documents, tmp_path / "files", preview)
    service = api_mod.KnowledgeApiService(nodes, documents, library, files, preview)
    monkeypatch.setattr(api_mod, "get_service", lambda: service)
    test_app = FastAPI()
    test_app.include_router(api_mod.router)
    test_client = TestClient(test_app)
    yield test_client, service
    nodes.close()
    documents.close()


def test_create_and_list_nodes(client):
    http, _ = client
    created = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "制度"},
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text
    folder = created.json()
    assert folder["kind"] == "folder"
    listed = http.get("/api/knowledge/nodes", params={"base_id": "b1"}, headers=ORIGIN)
    assert listed.status_code == 200
    assert [n["name"] for n in listed.json()["nodes"]] == ["制度"]


def test_upload_content_roundtrip_and_mtime_conflict(client):
    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={"file": ("说明.md", "第一版".encode(), "text/markdown")},
        headers=ORIGIN,
    )
    assert uploaded.status_code == 201, uploaded.text
    node = uploaded.json()
    content = http.get(f"/api/knowledge/nodes/{node['id']}/content", headers=ORIGIN).json()
    assert content["text"] == "第一版"
    saved = http.put(
        f"/api/knowledge/nodes/{node['id']}/content",
        json={"text": "第二版", "base_mtime": content["base_mtime"]},
        headers=ORIGIN,
    )
    assert saved.status_code == 200, saved.text
    conflict = http.put(
        f"/api/knowledge/nodes/{node['id']}/content",
        json={"text": "覆盖", "base_mtime": content["base_mtime"]},
        headers=ORIGIN,
    )
    assert conflict.status_code == 409


def test_upload_rejects_fake_office_file(client):
    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    rejected = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={"file": ("假.docx", b"not a zip", "application/octet-stream")},
        headers=ORIGIN,
    )
    assert rejected.status_code == 422


def test_rename_collision_gets_suffix(client):
    http, _ = client
    first = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "a.md"},
        headers=ORIGIN,
    ).json()
    second = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "b.md"},
        headers=ORIGIN,
    ).json()
    renamed = http.patch(
        f"/api/knowledge/nodes/{second['id']}",
        json={"name": "a.md"},
        headers=ORIGIN,
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "a (1).md"
    assert first["id"] != second["id"]


def test_move_into_descendant_conflicts(client):
    http, _ = client
    root = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "root"},
        headers=ORIGIN,
    ).json()
    child = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "child", "parent_id": root["id"]},
        headers=ORIGIN,
    ).json()
    moved = http.patch(
        f"/api/knowledge/nodes/{root['id']}",
        json={"parent_id": child["id"]},
        headers=ORIGIN,
    )
    assert moved.status_code == 409


def test_delete_cascades_documents(client):
    http, service = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "考勤.md", "parent_id": folder["id"]},
        headers=ORIGIN,
    ).json()
    result = service.documents.ingest_document("b1", "考勤.md", "考勤规则")
    service.documents.set_document_node(result.document_id, node["id"])
    deleted = http.delete(f"/api/knowledge/nodes/{folder['id']}", headers=ORIGIN)
    assert deleted.status_code == 200
    assert deleted.json()["documents"] == 1
    assert service.documents.get_document(result.document_id) is None


def test_preview_degrades_when_libreoffice_missing(client, monkeypatch):
    http, service = client
    monkeypatch.setattr("bandi.knowledge.files._find_soffice", lambda: None)
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    node = service.nodes.create_node("b1", "旧格式.doc", "docx", folder["id"])
    service.files.write(node.id, node.name, b"not a real docx")
    preview = http.get(f"/api/knowledge/nodes/{node.id}/preview", headers=ORIGIN)
    assert preview.status_code == 415


def test_pdf_preview_serves_source_without_libreoffice(client):
    http, service = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    payload = b"%PDF-1.7 fake"
    node = service.nodes.create_node("b1", "说明.pdf", "pdf", folder["id"])
    service.files.write(node.id, node.name, payload)
    preview = http.get(f"/api/knowledge/nodes/{node.id}/preview", headers=ORIGIN)
    assert preview.status_code == 200
    assert preview.content == payload


def test_download_returns_original_bytes(client):
    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={"file": ("数据.csv", b"a,b\n1,2\n", "text/csv")},
        headers=ORIGIN,
    ).json()
    download = http.get(f"/api/knowledge/nodes/{uploaded['id']}/download", headers=ORIGIN)
    assert download.status_code == 200
    assert download.content == b"a,b\n1,2\n"


def test_list_flat_returns_whole_tree(client):
    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "制度"},
        headers=ORIGIN,
    ).json()
    http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "考勤.md", "parent_id": folder["id"]},
        headers=ORIGIN,
    )
    flat = http.get("/api/knowledge/nodes", params={"base_id": "b1", "flat": 1}, headers=ORIGIN)
    assert flat.status_code == 200
    assert {n["name"] for n in flat.json()["nodes"]} == {"制度", "考勤.md"}
    children = http.get("/api/knowledge/nodes", params={"base_id": "b1"}, headers=ORIGIN).json()[
        "nodes"
    ]
    assert [n["name"] for n in children] == ["制度"]


def test_view_state_roundtrip_and_csv_only(client):
    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    csv_node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "csv", "name": "数据.csv", "parent_id": folder["id"]},
        headers=ORIGIN,
    ).json()
    state = {"filters": [{"col": 1, "op": "eq", "val": "上海"}], "rowHeight": "compact"}
    saved = http.put(
        f"/api/knowledge/nodes/{csv_node['id']}/view-state", json={"state": state}, headers=ORIGIN
    )
    assert saved.status_code == 200, saved.text
    loaded = http.get(f"/api/knowledge/nodes/{csv_node['id']}/view-state", headers=ORIGIN)
    assert loaded.json()["state"] == state

    md_node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "说明.md"},
        headers=ORIGIN,
    ).json()
    rejected = http.put(
        f"/api/knowledge/nodes/{md_node['id']}/view-state", json={"state": {}}, headers=ORIGIN
    )
    assert rejected.status_code == 415
    missing = http.get("/api/knowledge/nodes/kn_missing/view-state", headers=ORIGIN)
    assert missing.status_code == 404


def test_rename_syncs_document_filename(client):
    http, service = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={"file": ("旧名.md", "正文".encode(), "text/markdown")},
        headers=ORIGIN,
    ).json()
    document_id = service.documents.document_id_for_node(uploaded["id"])
    assert document_id
    renamed = http.patch(
        f"/api/knowledge/nodes/{uploaded['id']}", json={"name": "新名.md"}, headers=ORIGIN
    )
    assert renamed.status_code == 200
    document = service.documents.get_document(document_id)
    assert document is not None
    assert document.filename == "新名.md"
    assert document.title == "新名"


def test_asset_upload_and_fetch_roundtrip(client):
    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "说明.md", "parent_id": folder["id"]},
        headers=ORIGIN,
    ).json()
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    uploaded = http.post(
        f"/api/knowledge/nodes/{node['id']}/assets",
        files={"file": ("图 1.png", png, "image/png")},
        headers=ORIGIN,
    )
    assert uploaded.status_code == 201, uploaded.text
    payload = uploaded.json()
    fetched = http.get(payload["url"], headers=ORIGIN)
    assert fetched.status_code == 200
    assert fetched.content == png
    assert fetched.headers["content-type"].startswith("image/png")


def test_asset_rejects_unsupported_and_missing_node(client):
    http, _ = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "a.md"},
        headers=ORIGIN,
    ).json()
    rejected = http.post(
        f"/api/knowledge/nodes/{node['id']}/assets",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        headers=ORIGIN,
    )
    assert rejected.status_code == 422
    missing = http.post(
        "/api/knowledge/nodes/kn_missing/assets",
        files={"file": ("a.png", b"\x89PNG", "image/png")},
        headers=ORIGIN,
    )
    assert missing.status_code == 404


def test_xlsx_sheets_and_rows(client):
    from urllib.parse import quote

    import openpyxl

    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    workbook = openpyxl.Workbook()
    summary = workbook.active
    summary.title = "汇总"
    summary.append(["城市", "金额"])
    summary.append(["上海", 100])
    detail = workbook.create_sheet("明细")
    detail.append(["项目", "负责人"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={
            "file": (
                "数据.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        headers=ORIGIN,
    ).json()
    sheets = http.get(f"/api/knowledge/nodes/{uploaded['id']}/sheets", headers=ORIGIN)
    assert sheets.status_code == 200
    assert sheets.json()["sheets"] == ["汇总", "明细"]
    rows = http.get(f"/api/knowledge/nodes/{uploaded['id']}/sheets/{quote('汇总')}", headers=ORIGIN)
    assert rows.status_code == 200
    assert rows.json()["rows"][0] == ["城市", "金额"]
    assert rows.json()["rows"][1][0] == "上海"
    missing = http.get(
        f"/api/knowledge/nodes/{uploaded['id']}/sheets/{quote('不存在')}", headers=ORIGIN
    )
    assert missing.status_code == 404
    md = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "说明.md"},
        headers=ORIGIN,
    ).json()
    wrong = http.get(f"/api/knowledge/nodes/{md['id']}/sheets", headers=ORIGIN)
    assert wrong.status_code == 415


def test_pptx_slides_summary(client):
    import io

    from pptx import Presentation

    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    presentation = Presentation()
    first = presentation.slides.add_slide(presentation.slide_layouts[5])
    first.shapes.title.text = "封面"
    second = presentation.slides.add_slide(presentation.slide_layouts[5])
    second.shapes.title.text = "第二页"
    buffer = io.BytesIO()
    presentation.save(buffer)
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={
            "file": (
                "演示.pptx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            )
        },
        headers=ORIGIN,
    ).json()
    slides = http.get(f"/api/knowledge/nodes/{uploaded['id']}/slides", headers=ORIGIN)
    assert slides.status_code == 200
    payload = slides.json()
    assert payload["count"] == 2
    assert payload["slides"][0]["title"] == "封面"
    assert payload["slides"][1]["title"] == "第二页"
    md = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "说明.md"},
        headers=ORIGIN,
    ).json()
    wrong = http.get(f"/api/knowledge/nodes/{md['id']}/slides", headers=ORIGIN)
    assert wrong.status_code == 415


def test_new_text_node_has_empty_content(client):
    http, _ = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "新文档.md"},
        headers=ORIGIN,
    ).json()
    content = http.get(f"/api/knowledge/nodes/{node['id']}/content", headers=ORIGIN)
    assert content.status_code == 200, content.text
    payload = content.json()
    assert payload["text"] == ""
    assert payload["base_mtime"] > 0


def test_rename_moves_file_and_stays_readable(client):
    """改名后磁盘文件同步移动：内容/下载/签名都指向新文件名，旧文件不残留。"""
    http, service = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={"file": ("旧名.md", "正文 v1".encode(), "text/markdown")},
        headers=ORIGIN,
    ).json()
    renamed = http.patch(
        f"/api/knowledge/nodes/{uploaded['id']}", json={"name": "新名.md"}, headers=ORIGIN
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "新名.md"
    content = http.get(f"/api/knowledge/nodes/{uploaded['id']}/content", headers=ORIGIN)
    assert content.status_code == 200, content.text
    assert content.json()["text"] == "正文 v1"
    assert not service.files.path_for(uploaded["id"], "旧名.md").exists()
    download = http.get(f"/api/knowledge/nodes/{uploaded['id']}/download", headers=ORIGIN)
    assert download.status_code == 200
    assert download.content == "正文 v1".encode()


def test_docx_content_renders_html_without_libreoffice(client):
    """docx 走纯 Python 渲染：标题/段落/表格转 HTML，不依赖 LibreOffice。"""
    from docx import Document

    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    buffer = io.BytesIO()
    document = Document()
    document.add_heading("章标题", level=1)
    document.add_paragraph("正文段落")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "甲"
    table.rows[0].cells[1].text = "乙"
    document.save(buffer)
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={
            "file": (
                "说明.docx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=ORIGIN,
    ).json()
    content = http.get(f"/api/knowledge/nodes/{uploaded['id']}/content", headers=ORIGIN)
    assert content.status_code == 200, content.text
    payload = content.json()
    assert payload["kind"] == "docx"
    assert "<h1>章标题</h1>" in payload["html"]
    assert "<p>正文段落</p>" in payload["html"]
    assert "<table>" in payload["html"] and "甲" in payload["html"]


def test_root_upload_without_folder(client):
    """没有文件夹也能上传到库根目录。"""
    http, _ = client
    uploaded = http.post(
        "/api/knowledge/nodes/upload",
        data={"base_id": "b1", "parent_id": ""},
        files={"file": ("根目录.md", "根正文".encode(), "text/markdown")},
        headers=ORIGIN,
    )
    assert uploaded.status_code == 201, uploaded.text
    node = uploaded.json()
    assert node["parent_id"] == ""
    content = http.get(f"/api/knowledge/nodes/{node['id']}/content", headers=ORIGIN)
    assert content.json()["text"] == "根正文"


def test_upload_and_create_inherit_folder_employee(client):
    """文件夹指定员工后，新建/上传的子节点继承归属（检索收窄不失效）。"""
    http, service = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "仅甲", "employee_id": "emp-1"},
        headers=ORIGIN,
    ).json()
    child = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "子文档.md", "parent_id": folder["id"]},
        headers=ORIGIN,
    ).json()
    assert child["employee_id"] == "emp-1"
    uploaded = http.post(
        f"/api/knowledge/nodes/{folder['id']}/upload",
        files={"file": ("子资料.md", "内容".encode(), "text/markdown")},
        headers=ORIGIN,
    ).json()
    assert uploaded["employee_id"] == "emp-1"
    document_id = service.documents.document_id_for_node(uploaded["id"])
    assert document_id
    assert service.documents.get_document(document_id).node_employee_id == "emp-1"


def test_content_save_can_defer_ingest_and_reuses_document_id(client):
    """自动保存只写文件（ingest=False），显式 ingest 才建索引；文档 id 保持稳定。"""
    http, service = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "草稿.md"},
        headers=ORIGIN,
    ).json()
    saved = http.put(
        f"/api/knowledge/nodes/{node['id']}/content",
        json={"text": "第一版", "ingest": False},
        headers=ORIGIN,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["warning"] == "index deferred"
    assert service.documents.document_id_for_node(node["id"]) is None
    indexed = http.post(f"/api/knowledge/nodes/{node['id']}/ingest", headers=ORIGIN)
    assert indexed.status_code == 200, indexed.text
    first_id = indexed.json()["document_id"]
    assert first_id
    saved = http.put(
        f"/api/knowledge/nodes/{node['id']}/content",
        json={"text": "第二版 新内容", "ingest": True},
        headers=ORIGIN,
    )
    assert saved.status_code == 200
    assert service.documents.document_id_for_node(node["id"]) == first_id
    assert service.documents.get_document(first_id).chunk_count > 0


def test_delete_node_purges_preview_cache(client):
    """删除节点按渲染键（node-<id>）清缓存——不留孤儿 PDF。"""
    http, service = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "资料"},
        headers=ORIGIN,
    ).json()
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "pdf", "name": "说明.pdf", "parent_id": folder["id"]},
        headers=ORIGIN,
    ).json()
    cache_path = service.preview.cache_path(f"node-{node['id']}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"%PDF-1.7 cache")
    assert cache_path.exists()
    deleted = http.delete(f"/api/knowledge/nodes/{node['id']}", headers=ORIGIN)
    assert deleted.status_code == 200
    assert not cache_path.exists()


def test_link_node_rejects_non_http_url(client):
    """链接节点只接受 http(s)：脚本/本地协议直接 400。"""
    http, _ = client
    rejected = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "link", "name": "危险", "url": "javascript:alert(1)"},
        headers=ORIGIN,
    )
    assert rejected.status_code == 400
    accepted = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "link", "name": "官网", "url": "https://example.com"},
        headers=ORIGIN,
    )
    assert accepted.status_code == 201
    content = http.get(f"/api/knowledge/nodes/{accepted.json()['id']}/content", headers=ORIGIN)
    assert content.json()["url"] == "https://example.com"


def test_gbk_csv_upload_is_normalized_to_utf8(client):
    """Excel 导出的 GBK CSV 可上传：落盘转 UTF-8，内容读回不亂碼。"""
    http, service = client
    raw = "姓名,城市\n张三,上海\n".encode("gbk")
    uploaded = http.post(
        "/api/knowledge/nodes/upload",
        data={"base_id": "b1", "parent_id": ""},
        files={"file": ("名单.csv", raw, "text/csv")},
        headers=ORIGIN,
    )
    assert uploaded.status_code == 201, uploaded.text
    node = uploaded.json()
    content = http.get(f"/api/knowledge/nodes/{node['id']}/content", headers=ORIGIN)
    assert content.status_code == 200, content.text
    assert "张三" in content.json()["text"]
    assert service.files.read(node["id"], node["name"]).decode("utf-8").startswith("姓名,城市")


def test_knowledge_upload_path_allows_larger_body():
    """资料库上传/资产走 55MB 请求体上限，不再被全局 10MB 压死。"""
    from bandi.api.app import _body_limit_for_path

    assert _body_limit_for_path("/api/knowledge/nodes/upload") >= 50 * 1024 * 1024
    assert _body_limit_for_path("/api/knowledge/nodes/kn_x/assets") >= 50 * 1024 * 1024
    assert _body_limit_for_path("/api/sessions") < _body_limit_for_path(
        "/api/knowledge/nodes/upload"
    )


def test_large_content_ingest_runs_in_background(client):
    http, service = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "大文件.md"},
        headers=ORIGIN,
    ).json()
    text = "# 大文件\n" + "内容行\n" * 80000
    saved = http.put(
        f"/api/knowledge/nodes/{node['id']}/content",
        json={"text": text, "ingest": True},
        headers=ORIGIN,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["warning"] == "index scheduled"
    for _ in range(100):
        if service.documents.document_id_for_node(node["id"]):
            break
        time.sleep(0.1)
    assert service.documents.document_id_for_node(node["id"])
    assert service.documents.document_id_for_node(node["id"]).startswith("kdoc:")


def test_large_ingest_endpoint_reports_scheduled(client):
    http, service = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "再大文件.md"},
        headers=ORIGIN,
    ).json()
    text = "第二份\n" * 90000
    assert (
        http.put(
            f"/api/knowledge/nodes/{node['id']}/content",
            json={"text": text, "ingest": False},
            headers=ORIGIN,
        ).status_code
        == 200
    )
    started = http.post(f"/api/knowledge/nodes/{node['id']}/ingest", headers=ORIGIN)
    assert started.status_code == 200, started.text
    assert started.json()["scheduled"] is True
    for _ in range(100):
        if service.documents.document_id_for_node(node["id"]):
            break
        time.sleep(0.1)
    assert service.documents.document_id_for_node(node["id"])


def test_large_upload_schedules_background_index(client):
    http, service = client
    data = ("# 大上传\n" + "行内容\n" * 60000).encode("utf-8")
    uploaded = http.post(
        "/api/knowledge/nodes/upload",
        data={"base_id": "b1"},
        files={"file": ("大上传.md", data, "text/markdown")},
        headers=ORIGIN,
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["scheduled"] is True
    for _ in range(100):
        if service.documents.document_id_for_node(uploaded.json()["id"]):
            break
        time.sleep(0.1)
    assert service.documents.document_id_for_node(uploaded.json()["id"])


def test_force_save_overwrites_stale_mtime(client):
    http, service = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "冲突.md"},
        headers=ORIGIN,
    ).json()
    assert (
        http.put(
            f"/api/knowledge/nodes/{node['id']}/content",
            json={"text": "本地版本", "base_mtime": 1, "ingest": False},
            headers=ORIGIN,
        ).status_code
        == 409
    )
    stale = http.put(
        f"/api/knowledge/nodes/{node['id']}/content",
        json={"text": "本地版本", "base_mtime": 1, "ingest": False, "force": True},
        headers=ORIGIN,
    )
    assert stale.status_code == 200, stale.text
    assert (
        http.get(f"/api/knowledge/nodes/{node['id']}/content", headers=ORIGIN).json()["text"]
        == "本地版本"
    )


def test_link_snapshot_endpoint_stores_and_serves_html(client, monkeypatch):
    from bandi.knowledge.link_snapshot import Snapshot

    http, service = client
    link = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "link", "name": "官网", "url": "https://example.com"},
        headers=ORIGIN,
    ).json()
    monkeypatch.setattr(
        api_mod,
        "fetch_snapshot",
        lambda url: Snapshot(
            html="<article><h1>快照标题</h1><p>快照正文</p></article>",
            text="快照标题 快照正文",
            title="快照标题",
            final_url=url,
            size=42,
        ),
    )
    saved = http.post(f"/api/knowledge/nodes/{link['id']}/snapshot", headers=ORIGIN)
    assert saved.status_code == 200, saved.text
    assert saved.json()["title"] == "快照标题"
    content = http.get(f"/api/knowledge/nodes/{link['id']}/content", headers=ORIGIN).json()
    assert "快照标题" in content["snapshot_html"]
    assert content["snapshot_at"] > 0
    assert service.documents.document_id_for_node(link["id"])


def test_link_snapshot_rejects_private_or_non_link(client, monkeypatch):
    from bandi.knowledge.link_snapshot import SnapshotError

    http, _ = client
    folder = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "folder", "name": "目录"},
        headers=ORIGIN,
    ).json()
    assert (
        http.post(f"/api/knowledge/nodes/{folder['id']}/snapshot", headers=ORIGIN).status_code
        == 422
    )
    link = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "link", "name": "内网", "url": "http://10.0.0.8/"},
        headers=ORIGIN,
    ).json()

    def reject(url: str):
        raise SnapshotError("不允许抓取内网或本机地址")

    monkeypatch.setattr(api_mod, "fetch_snapshot", reject)
    failed = http.post(f"/api/knowledge/nodes/{link['id']}/snapshot", headers=ORIGIN)
    assert failed.status_code == 422
    assert "内网" in failed.json()["detail"]


def test_delete_large_document_cleans_fts(client):
    http, service = client
    node = http.post(
        "/api/knowledge/nodes",
        json={"base_id": "b1", "kind": "md", "name": "大文档.md"},
        headers=ORIGIN,
    ).json()
    large = "# 大文档\n" + "内容行\n" * 90000
    service.library.reindex(service.nodes.get_node(node["id"]), text=large)
    assert service.documents.document_id_for_node(node["id"])
    deleted = http.delete(f"/api/knowledge/nodes/{node['id']}", headers=ORIGIN)
    assert deleted.status_code == 200, deleted.text
    assert service.documents.document_id_for_node(node["id"]) is None
