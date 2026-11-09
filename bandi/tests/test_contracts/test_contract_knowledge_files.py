# 功能：资料库文件准入/预览/级联契约——魔数、压缩炸弹、串行转换、缓存回收、删除级联。

from __future__ import annotations

import io
import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest

from bandi.knowledge import files as files_mod
from bandi.knowledge.files import (
    PreviewService,
    PreviewUnavailableError,
    SuspiciousFileError,
    UnsupportedFileTypeError,
    detect_kind,
)
from bandi.knowledge.library import KnowledgeLibraryService
from bandi.knowledge.nodes import KnowledgeNodeStore
from bandi.knowledge.store import KnowledgeStore


def _zip_bytes(entries: dict[str, bytes], content_types: bool = True) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if content_types:
            archive.writestr("[Content_Types].xml", "<Types/>")
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.fixture()
def nodes(tmp_path) -> KnowledgeNodeStore:
    store = KnowledgeNodeStore(tmp_path / "knowledge.db")
    yield store
    store.close()


def test_detect_kind_accepts_valid_magic_and_rejects_mismatch():
    docx = _zip_bytes({"word/document.xml": b"<w/>"})
    assert detect_kind("制度.docx", docx) == "docx"
    assert detect_kind("说明.pdf", b"%PDF-1.7\n...") == "pdf"
    assert detect_kind("笔记.md", "中文正文".encode()) == "md"
    assert detect_kind("页面.html", b"<!doctype html><p>x</p>") == "html"
    with pytest.raises(SuspiciousFileError):
        detect_kind("假文档.docx", b"just text")
    with pytest.raises(SuspiciousFileError):
        detect_kind("假.pdf", b"PK\x03\x04")
    with pytest.raises(SuspiciousFileError):
        detect_kind("二进制.md", b"\x00\x01\x02")
    with pytest.raises(UnsupportedFileTypeError):
        detect_kind("evil.exe", b"MZ")


def test_detect_kind_rejects_office_zip_without_content_types():
    payload = _zip_bytes({"word/document.xml": b"<w/>"}, content_types=False)
    with pytest.raises(SuspiciousFileError):
        detect_kind("无清单.docx", payload)


def test_detect_kind_enforces_entry_and_expansion_limits(monkeypatch):
    many = _zip_bytes({f"part{i}.xml": b"<x/>" for i in range(8)})
    monkeypatch.setattr(files_mod, "ZIP_MAX_ENTRIES", 3)
    with pytest.raises(SuspiciousFileError):
        detect_kind("条目过多.docx", many)
    monkeypatch.setattr(files_mod, "ZIP_MAX_ENTRIES", 2_000)
    big = _zip_bytes({"word/document.xml": b"0" * (2 * 1024 * 1024)})
    monkeypatch.setattr(files_mod, "ZIP_MAX_UNCOMPRESSED_BYTES", 1024 * 1024)
    with pytest.raises(SuspiciousFileError):
        detect_kind("解压炸弹.docx", big)


def test_detect_kind_rejects_oversize(monkeypatch):
    monkeypatch.setattr(files_mod, "MAX_UPLOAD_BYTES", 16)
    with pytest.raises(files_mod.FileValidationError):
        detect_kind("大.md", b"x" * 64)


def test_preview_conversions_are_serialized(tmp_path):
    state = {"active": 0, "max": 0}
    guard = threading.Lock()

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
        with guard:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.12)
        outdir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        (outdir / f"{source.stem}.pdf").write_bytes(b"%PDF-1.7 fake")
        with guard:
            state["active"] -= 1
        return subprocess.CompletedProcess(command, 0, b"", b"")

    service = PreviewService(tmp_path / "cache", runner=runner, soffice_path="soffice")
    threads = [
        threading.Thread(target=service.render, args=("doc-a", tmp_path / "a.docx"))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert state["max"] == 1
    assert (tmp_path / "cache" / "doc-a.pdf").read_bytes().startswith(b"%PDF")


def test_preview_unavailable_on_timeout_or_missing_soffice(tmp_path):
    def timeout_runner(command: list[str], timeout: float):
        raise subprocess.TimeoutExpired(command, timeout)

    service = PreviewService(tmp_path / "c1", runner=timeout_runner, soffice_path="soffice")
    with pytest.raises(PreviewUnavailableError):
        service.render("d1", tmp_path / "x.docx")
    missing = PreviewService(tmp_path / "c2", soffice_path="")
    with pytest.raises(PreviewUnavailableError):
        missing.render("d2", tmp_path / "x.docx")


def test_preview_cache_cleanup_removes_oldest_and_purge(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for index, name in enumerate(["old.pdf", "mid.pdf", "new.pdf"]):
        path = cache / name
        path.write_bytes(b"x" * 100)
        stamp = time.time() - (10 - index)
        os.utime(path, (stamp, stamp))
    service = PreviewService(cache, max_bytes=150)
    removed = service.cleanup()
    assert removed == 2
    assert (cache / "new.pdf").exists()
    assert not (cache / "old.pdf").exists()
    (cache / "doc9.pdf").write_bytes(b"x")
    assert service.purge("doc9") == 1


def test_library_upload_writes_file_and_dedupes_name(tmp_path):
    nodes = KnowledgeNodeStore(tmp_path / "k.db")
    documents = KnowledgeStore(tmp_path / "k.db")
    service = KnowledgeLibraryService(nodes, documents, tmp_path / "files")
    try:
        first = service.upload("b1", "", "报告.md", "第一版".encode())
        second = service.upload("b1", "", "报告.md", "第二版".encode())
        assert first.name == "报告.md"
        assert second.name == "报告 (1).md"
        assert (tmp_path / "files" / first.id / "报告.md").read_text(encoding="utf-8") == "第一版"
        assert first.size == len("第一版".encode())
    finally:
        nodes.close()
        documents.close()


def test_library_delete_cascades_documents_chunks_fts_and_files(tmp_path):
    nodes = KnowledgeNodeStore(tmp_path / "k.db")
    documents = KnowledgeStore(tmp_path / "k.db")
    preview = PreviewService(tmp_path / "cache", soffice_path="")
    service = KnowledgeLibraryService(nodes, documents, tmp_path / "files", preview)
    try:
        folder = nodes.create_node("b1", "制度", "folder")
        node = nodes.create_node("b1", "考勤.md", "md", folder.id)
        target = tmp_path / "files" / node.id / "考勤.md"
        target.parent.mkdir(parents=True)
        target.write_text("考勤规则", encoding="utf-8")
        result = documents.ingest_document("b1", "考勤.md", "考勤规则正文")
        documents.set_document_node(result.document_id, node.id)
        preview.cache_path(result.document_id).write_bytes(b"%PDF")
        assert documents.list_chunks(result.document_id)

        outcome = service.delete_node(folder.id)

        assert outcome["nodes"] == 2
        assert outcome["documents"] == 1
        assert nodes.get_node(node.id) is None
        assert documents.get_document(result.document_id) is None
        assert documents.list_chunks(result.document_id) == []
        assert not preview.cache_path(result.document_id).exists()
        assert not (tmp_path / "files" / node.id).exists()
    finally:
        nodes.close()
        documents.close()
