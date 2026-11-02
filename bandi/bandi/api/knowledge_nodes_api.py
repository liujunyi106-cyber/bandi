# 功能：资料库节点 API——树查询、新建、重命名/移动/员工、上传、内容读写、预览与下载。

from __future__ import annotations

import json
import mimetypes
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from bandi.knowledge.docx_html import render_docx_html
from bandi.knowledge.files import (
    MAX_UPLOAD_BYTES,
    FileValidationError,
    PreviewService,
    PreviewUnavailableError,
    detect_kind,
)
from bandi.knowledge.library import BACKGROUND_INDEX_BYTES, KnowledgeLibraryService
from bandi.knowledge.link_snapshot import SnapshotError, fetch_snapshot
from bandi.knowledge.nodes import KnowledgeNodeStore
from bandi.knowledge.storage import ASSET_MAX_BYTES, NodeFileStore
from bandi.knowledge.store import KnowledgeStore, get_shared_store

HTTP_BAD_REQUEST = 400
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_PAYLOAD_TOO_LARGE = 413
HTTP_UNSUPPORTED_MEDIA = 415
HTTP_UNPROCESSABLE = 422
VIEW_STATE_MAX_BYTES = 64 * 1024
ASSET_SUFFIXES = frozenset(
    {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "svg",
        "mp4",
        "webm",
        "mp3",
        "wav",
        "pdf",
        "txt",
        "md",
        "csv",
        "docx",
        "xlsx",
        "pptx",
        "zip",
    }
)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class NodeCreate(BaseModel):
    base_id: str = Field(min_length=1)
    kind: str = "md"
    name: str = Field(min_length=1)
    parent_id: str = ""
    employee_id: str | None = None
    url: str = ""


class NodePatch(BaseModel):
    name: str | None = None
    parent_id: str | None = None
    employee_id: str | None = None


class ContentPut(BaseModel):
    text: str
    base_mtime: float | None = None
    ingest: bool = True
    force: bool = False


class ViewStatePut(BaseModel):
    state: dict[str, Any] = Field(default_factory=dict)


class KnowledgeApiService:
    # 功能：API 依赖聚合——节点树、文档索引、文件落盘、预览缓存共用一套实例。
    def __init__(
        self,
        nodes: KnowledgeNodeStore,
        documents: KnowledgeStore,
        library: KnowledgeLibraryService,
        files: NodeFileStore,
        preview: PreviewService,
    ) -> None:
        self.nodes = nodes
        self.documents = documents
        self.library = library
        self.files = files
        self.preview = preview


def build_service(db_path: Path, files_root: Path, rendered_root: Path) -> KnowledgeApiService:
    nodes = KnowledgeNodeStore(db_path)
    documents = get_shared_store()
    files = NodeFileStore(files_root)
    preview = PreviewService(rendered_root)
    library = KnowledgeLibraryService(nodes, documents, files_root, preview, files=files)
    return KnowledgeApiService(nodes, documents, library, files, preview)


@lru_cache(maxsize=1)
def get_service() -> KnowledgeApiService:
    # 功能：进程级服务单例——沿用 ~/.bandi 路径，API 与工具面共享索引库。
    home = Path.home() / ".bandi"
    data_dir = home / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return build_service(
        data_dir / "knowledge.db",
        home / "knowledge" / "files",
        home / "knowledge" / "rendered",
    )


def _validate_link_url(url: str) -> None:
    # 功能：链接节点只接受 http(s) 绝对地址，拒绝脚本/本地协议。
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(
            status_code=HTTP_BAD_REQUEST, detail="link url must be an absolute http(s) url"
        )


def _node_payload(node: Any) -> dict[str, Any]:
    return {
        "id": node.id,
        "base_id": node.base_id,
        "parent_id": node.parent_id,
        "kind": node.kind,
        "name": node.name,
        "sort_order": node.sort_order,
        "employee_id": node.employee_id,
        "size": node.size,
        "url": node.url,
        "updated_at": node.updated_at,
    }


@router.get("/nodes")
def list_nodes(base_id: str, parent_id: str = "", q: str = "", flat: int = 0) -> dict[str, Any]:
    if not base_id.strip():
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="base_id is required")
    service = get_service()
    nodes = (
        service.nodes.list_tree(base_id)
        if flat
        else service.nodes.list_children(base_id, parent_id)
    )
    query = q.strip().lower()
    if query:
        nodes = [node for node in nodes if query in node.name.lower()][:200]
    return {"nodes": [_node_payload(node) for node in nodes]}


@router.post("/nodes", status_code=201)
def create_node(body: NodeCreate) -> dict[str, Any]:
    service = get_service()
    if body.kind == "link":
        _validate_link_url(body.url)
    employee_id = body.employee_id
    if employee_id is None and "employee_id" not in body.model_fields_set and body.parent_id:
        parent = service.nodes.get_node(body.parent_id)
        if parent is not None:
            employee_id = parent.employee_id
    try:
        node = service.nodes.create_node(
            body.base_id,
            body.name,
            body.kind,
            body.parent_id,
            employee_id=employee_id,
            url=body.url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail=str(exc)) from exc
    if node.kind in {"md", "csv", "html"}:
        service.files.write(node.id, node.name, b"")
        node = service.nodes.set_storage(node.id, f"{node.id}/{node.name}", 0) or node
    return _node_payload(node)


@router.patch("/nodes/{node_id}")
def patch_node(node_id: str, body: NodePatch) -> dict[str, Any]:
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    try:
        if body.employee_id is not None:
            service.nodes.set_employee(node_id, body.employee_id or None)
        if body.parent_id is not None and body.parent_id != node.parent_id:
            node = service.nodes.move_node(node_id, body.parent_id)
        if body.name is not None and body.name != node.name:
            node = service.library.rename(node_id, body.name)
    except ValueError as exc:
        status = HTTP_CONFLICT if "descendants" in str(exc) else HTTP_BAD_REQUEST
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    updated = service.nodes.get_node(node_id)
    assert updated is not None
    return _node_payload(updated)


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str) -> dict[str, int]:
    service = get_service()
    if service.nodes.get_node(node_id) is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    return service.library.delete_node(node_id)


@router.post("/nodes/upload", status_code=201)
async def upload_root_file(
    base_id: str = Form(...),
    parent_id: str = Form(""),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    # 功能：根目录/指定文件夹上传——未建文件夹时也能直接落文件。
    service = get_service()
    if not base_id.strip():
        raise HTTPException(status_code=HTTP_BAD_REQUEST, detail="base_id is required")
    employee_id = None
    if parent_id:
        parent = service.nodes.get_node(parent_id)
        if parent is None or parent.kind != "folder" or parent.base_id != base_id:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="folder not found")
        employee_id = parent.employee_id
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        detect_kind(file.filename or "", data)
    except FileValidationError as exc:
        status = HTTP_PAYLOAD_TOO_LARGE if "large" in str(exc) else HTTP_UNPROCESSABLE
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    node = service.library.upload(
        base_id,
        parent_id,
        file.filename or "未命名",
        data,
        employee_id=employee_id,
    )
    return {**_node_payload(node), "scheduled": (node.size or 0) > BACKGROUND_INDEX_BYTES}


@router.post("/nodes/{node_id}/upload", status_code=201)
async def upload_node_file(
    node_id: str,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    # 功能：挂到已存在文件夹下的上传——校验魔数/大小后落盘并建文件节点。
    service = get_service()
    folder = service.nodes.get_node(node_id)
    if folder is None or folder.kind != "folder":
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="folder not found")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        detect_kind(file.filename or "", data)
    except FileValidationError as exc:
        status = HTTP_PAYLOAD_TOO_LARGE if "large" in str(exc) else HTTP_UNPROCESSABLE
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    node = service.library.upload(
        folder.base_id,
        folder.id,
        file.filename or "未命名",
        data,
        employee_id=folder.employee_id,
    )
    return {**_node_payload(node), "scheduled": (node.size or 0) > BACKGROUND_INDEX_BYTES}


@router.get("/nodes/{node_id}/content")
def get_content(node_id: str) -> dict[str, Any]:
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind in {"md", "csv"}:
        try:
            raw = service.files.read(node.id, node.name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="file missing") from exc
        return {
            "kind": node.kind,
            "text": raw.decode("utf-8-sig", errors="replace"),
            "base_mtime": service.files.mtime(node.id, node.name),
        }
    if node.kind == "html":
        try:
            raw = service.files.read(node.id, node.name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="file missing") from exc
        return {"kind": "html", "html": raw.decode("utf-8-sig", errors="replace")}
    if node.kind == "docx":
        try:
            raw = service.files.read(node.id, node.name)
            html = render_docx_html(raw)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="file missing") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=HTTP_UNSUPPORTED_MEDIA, detail=f"docx unreadable: {exc}"
            ) from exc
        return {
            "kind": "docx",
            "html": html,
            "base_mtime": service.files.mtime(node.id, node.name),
        }
    if node.kind in {"xlsx", "pptx", "pdf"}:
        return {
            "kind": node.kind,
            "pdf_url": f"/api/knowledge/nodes/{node.id}/preview",
        }
    if node.kind == "link":
        payload: dict[str, Any] = {"kind": "link", "url": node.url}
        snapshot = service.library.snapshot(node)
        if snapshot is not None:
            payload["snapshot_html"], payload["snapshot_at"] = snapshot
        return payload
    raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="preview not supported")


@router.put("/nodes/{node_id}/content")
def put_content(node_id: str, body: ContentPut) -> dict[str, Any]:
    # 功能：写 md/csv 正文——mtime 不一致返回 409，成功时按节流写 .bak，按需重建索引。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind not in {"md", "csv"}:
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="text editing not supported")
    current = service.files.mtime(node.id, node.name)
    if not body.force and body.base_mtime is not None and abs(current - body.base_mtime) > 0.001:
        raise HTTPException(status_code=HTTP_CONFLICT, detail="file changed on disk")
    data = body.text.encode("utf-8")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=HTTP_PAYLOAD_TOO_LARGE, detail="content too large")
    _, _, saved_at = service.files.write(node.id, node.name, data)
    service.nodes.set_storage(node.id, f"{node.id}/{node.name}", len(data))
    warning = None
    if body.ingest:
        if len(data) > BACKGROUND_INDEX_BYTES:
            service.library.reindex_async(node, text=body.text)
            warning = "index scheduled"
        else:
            service.library.reindex(node, text=body.text)
    else:
        warning = "index deferred"
    return {"saved_at": saved_at, "base_mtime": saved_at, "warning": warning}


@router.post("/nodes/{node_id}/ingest")
def ingest_node(
    node_id: str,
) -> dict[str, Any]:  # 功能：按需重建节点索引——编辑停手/切换文件时调用，避免每次自动保存都重建。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind == "folder":
        raise HTTPException(status_code=HTTP_UNPROCESSABLE, detail="folder has no content")
    text = None
    if node.kind in {"md", "csv", "html"}:
        try:
            text = service.files.read(node.id, node.name).decode("utf-8-sig", errors="replace")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="file missing") from exc
    size = node.size or 0
    if text is not None:
        size = max(size, len(text.encode("utf-8")))
    if size > BACKGROUND_INDEX_BYTES:
        service.library.reindex_async(node, text=text)
        return {
            "indexed": False,
            "scheduled": True,
            "document_id": service.documents.document_id_for_node(node.id),
        }
    result = service.library.reindex(node, text=text)
    document_id = service.documents.document_id_for_node(node.id)
    return {"indexed": result is not None, "document_id": document_id, "scheduled": False}


@router.post("/nodes/{node_id}/snapshot")
def snapshot_node(node_id: str) -> dict[str, Any]:
    # 功能：链接节点手动抓取正文——公网校验、固定 IP 抓取、清洗保存并 ingest。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind != "link" or not node.url:
        raise HTTPException(status_code=HTTP_UNPROCESSABLE, detail="link node required")
    try:
        snapshot = fetch_snapshot(node.url)
    except SnapshotError as exc:
        raise HTTPException(status_code=HTTP_UNPROCESSABLE, detail=str(exc)) from exc
    saved_at = service.library.save_snapshot(node, snapshot.html)
    size = len(snapshot.text.encode("utf-8"))
    if size > BACKGROUND_INDEX_BYTES:
        service.library.reindex_async(node, text=snapshot.text)
        scheduled = True
    else:
        service.library.reindex(node, text=snapshot.text)
        scheduled = False
    return {
        "ok": True,
        "title": snapshot.title,
        "bytes": snapshot.size,
        "snapshot_at": saved_at,
        "scheduled": scheduled,
        "document_id": service.documents.document_id_for_node(node.id),
    }


@router.get("/nodes/{node_id}/view-state")
def get_view_state(node_id: str) -> dict[str, Any]:
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    return {"state": service.nodes.get_view(node_id) or {}}


@router.put("/nodes/{node_id}/view-state")
def put_view_state(node_id: str, body: ViewStatePut) -> dict[str, Any]:
    # 功能：保存 CSV 视图态（筛选/分组/排序/行高/填色），只服务 csv 节点。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind != "csv":
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="view state is csv-only")
    encoded = json.dumps(body.state, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > VIEW_STATE_MAX_BYTES:
        raise HTTPException(status_code=HTTP_PAYLOAD_TOO_LARGE, detail="view state too large")
    service.nodes.set_view(node_id, node.kind, body.state)
    return {"state": body.state, "saved": True}


@router.post("/nodes/{node_id}/assets", status_code=201)
async def upload_asset(node_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    # 功能：编辑器媒体块上传——图片/视频/附件落 node/assets，返回可直接引用的 URL。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    data = await file.read(ASSET_MAX_BYTES + 1)
    if len(data) > ASSET_MAX_BYTES:
        raise HTTPException(status_code=HTTP_PAYLOAD_TOO_LARGE, detail="asset too large")
    filename = file.filename or "asset"
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in ASSET_SUFFIXES:
        raise HTTPException(status_code=HTTP_UNPROCESSABLE, detail="unsupported asset type")
    saved = service.files.save_asset(node.id, filename, data)
    return {
        "name": saved,
        "size": len(data),
        "url": f"/api/knowledge/nodes/{node.id}/assets/{quote(saved)}",
    }


@router.get("/nodes/{node_id}/assets/{name}")
def get_asset(node_id: str, name: str) -> FileResponse:
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    path = service.files.asset_path(node.id, name)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="asset not found")
    media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/nodes/{node_id}/slides")
def list_slides(node_id: str) -> dict[str, Any]:
    # 功能：pptx 逐页摘要——缩略图列表与翻页用；非 pptx 返回 415。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind != "pptx":
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="slides are pptx-only")
    try:
        from pptx import Presentation

        presentation = Presentation(service.files.path_for(node.id, node.name))
        slides: list[dict[str, Any]] = []
        for index, slide in enumerate(presentation.slides, start=1):
            texts = [
                str(getattr(shape, "text", "")).strip()
                for shape in slide.shapes
                if str(getattr(shape, "text", "")).strip()
            ]
            slides.append(
                {
                    "index": index,
                    "title": texts[0] if texts else f"第 {index} 页",
                    "text": "\n".join(texts),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 损坏文件按不可预览处理
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail=f"pptx unreadable: {exc}")
    return {"count": len(slides), "slides": slides}


@router.get("/nodes/{node_id}/sheets")
def list_sheets(node_id: str) -> dict[str, Any]:
    # 功能：xlsx 工作表清单——前端切换页签用；非 xlsx 返回 415。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind != "xlsx":
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="sheets are xlsx-only")
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(
            service.files.path_for(node.id, node.name), read_only=True, data_only=True
        )
        names = list(workbook.sheetnames)
        workbook.close()
    except Exception as exc:  # noqa: BLE001 - 损坏文件按不可预览处理
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail=f"xlsx unreadable: {exc}")
    return {"sheets": names}


@router.get("/nodes/{node_id}/sheets/{sheet_name}")
def get_sheet(node_id: str, sheet_name: str, limit: int = 200) -> dict[str, Any]:
    # 功能：读取工作表前 N 行（上限 50 列），返回二维字符串网格。
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind != "xlsx":
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="sheets are xlsx-only")
    capped = max(1, min(limit, 500))
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(
            service.files.path_for(node.id, node.name), read_only=True, data_only=True
        )
        if sheet_name not in workbook.sheetnames:
            raise HTTPException(status_code=HTTP_NOT_FOUND, detail="sheet not found")
        sheet = workbook[sheet_name]
        rows: list[list[str]] = []
        truncated = False
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= capped:
                truncated = True
                break
            rows.append(["" if value is None else str(value) for value in row[:50]])
        width = max((len(row) for row in rows), default=0)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        workbook.close()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 损坏文件按不可预览处理
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail=f"xlsx unreadable: {exc}")
    return {"name": sheet_name, "rows": normalized, "truncated": truncated}


@router.get("/nodes/{node_id}/preview")
def get_preview(node_id: str) -> FileResponse:
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    if node.kind not in {"docx", "xlsx", "pptx", "pdf"}:
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail="preview not supported")
    source = service.files.path_for(node.id, node.name)
    if not source.exists():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="file missing")
    if node.kind == "pdf":
        return FileResponse(source, media_type="application/pdf", filename=node.name)
    try:
        target = service.preview.render(f"node-{node.id}", source)
    except PreviewUnavailableError as exc:
        raise HTTPException(status_code=HTTP_UNSUPPORTED_MEDIA, detail=str(exc)) from exc
    return FileResponse(target, media_type="application/pdf", filename=f"{node.name}.pdf")


@router.get("/nodes/{node_id}/download")
def download_node(node_id: str) -> FileResponse:
    service = get_service()
    node = service.nodes.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="node not found")
    path = service.files.path_for(node.id, node.name)
    if not path.exists():
        raise HTTPException(status_code=HTTP_NOT_FOUND, detail="file missing")
    return FileResponse(path, filename=node.name)
