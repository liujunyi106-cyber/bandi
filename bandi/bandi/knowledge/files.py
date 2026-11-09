# 功能：资料库文件准入与预览——扩展名+魔数校验、压缩炸弹上限、LibreOffice 串行转换、预览缓存 LRU。

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ZIP_MAX_ENTRIES = 2_000
ZIP_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
PREVIEW_CACHE_MAX_BYTES = 512 * 1024 * 1024
PREVIEW_TIMEOUT_SEC = 90
CONTENT_TYPES_NAME = "[content_types].xml"

_EXT_KIND: dict[str, str] = {
    "md": "md",
    "markdown": "md",
    "txt": "md",
    "csv": "csv",
    "docx": "docx",
    "doc": "docx",
    "xlsx": "xlsx",
    "xls": "xlsx",
    "pptx": "pptx",
    "ppt": "pptx",
    "pdf": "pdf",
    "html": "html",
    "htm": "html",
}
_LEGACY_OLE_EXT = frozenset({"doc", "xls", "ppt"})
_TEXT_KINDS = frozenset({"md", "csv", "html"})
_OFFICE_ZIP_KINDS = frozenset({"docx", "xlsx", "pptx"})
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

Runner = Callable[[list[str], float], subprocess.CompletedProcess]


class FileValidationError(ValueError):
    """功能：上传文件未通过准入校验（类型/大小/内容不符）。"""


class UnsupportedFileTypeError(FileValidationError):
    """功能：扩展名不在白名单内。"""


class SuspiciousFileError(FileValidationError):
    """功能：扩展名与文件头不一致或压缩结构异常。"""


class PreviewUnavailableError(RuntimeError):
    """功能：Office 预览不可用（未安装 LibreOffice 或转换超时）。"""


def detect_kind(filename: str, data: bytes) -> str:
    # 功能：返回规范化的节点类型——扩展名白名单 + 文件头魔数 + 压缩炸弹上限。
    if len(data) > MAX_UPLOAD_BYTES:
        raise FileValidationError("file too large")
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").strip().lower()
    kind = _EXT_KIND.get(ext)
    if not kind:
        raise UnsupportedFileTypeError(f"unsupported file type: {ext or '(none)'}")
    if ext in _LEGACY_OLE_EXT:
        if not data.startswith(_OLE_MAGIC):
            raise SuspiciousFileError("legacy office file magic mismatch")
        return kind
    if kind in _OFFICE_ZIP_KINDS:
        _check_office_zip(data)
        return kind
    if kind == "pdf":
        if not data.startswith(_PDF_MAGIC):
            raise SuspiciousFileError("pdf magic mismatch")
        return kind
    _check_text(data)
    return kind


def _check_text(data: bytes) -> None:
    if decode_text(data) is None:
        raise SuspiciousFileError("text file is not utf-8/gbk")


def decode_text(data: bytes) -> str | None:
    # 功能：文本文件按 utf-8-sig → gbk 探测解码，含 NUL 或两种都失败返回 None。
    if b"\x00" in data:
        return None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def normalize_text(data: bytes) -> bytes:
    # 功能：文本文件统一转成无 BOM 的 UTF-8 落盘，兼容 Excel 导出的 GBK/带 BOM CSV。
    text = decode_text(data)
    if text is None:
        raise SuspiciousFileError("text file is not utf-8/gbk")
    return text.encode("utf-8")


def _check_office_zip(data: bytes) -> None:
    # 功能：Office 文件必须是含 [Content_Types].xml 的 zip，且条目数/解压体积不超限。
    if not data.startswith(_ZIP_MAGIC):
        raise SuspiciousFileError("office file magic mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > ZIP_MAX_ENTRIES:
                raise SuspiciousFileError("office archive has too many entries")
            total = sum(int(info.file_size) for info in entries)
            if total > ZIP_MAX_UNCOMPRESSED_BYTES:
                raise SuspiciousFileError("office archive expands beyond limit")
            names = {info.filename.lower() for info in entries}
    except zipfile.BadZipFile as exc:
        raise SuspiciousFileError("office file is not a valid zip") from exc
    if CONTENT_TYPES_NAME not in names:
        raise SuspiciousFileError("office package missing [Content_Types].xml")

    def tell(self) -> int:
        return self._offset


class PreviewService:
    # 功能：Office 文档转 PDF 预览——全局单锁串行调用 soffice，超时降级，缓存按容量 LRU 清理。
    _convert_lock = threading.Lock()

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        max_bytes: int = PREVIEW_CACHE_MAX_BYTES,
        timeout: float = PREVIEW_TIMEOUT_SEC,
        soffice_path: str | None = None,
        runner: Runner | None = None,
    ) -> None:
        self._cache = Path(cache_dir)
        self._cache.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._soffice_path = soffice_path
        self._runner = runner or _default_runner

    def cache_path(self, document_id: str) -> Path:
        # 功能：预览缓存的落点（文档 id 归一后的 pdf 路径）。
        return self._cache / _cache_name(document_id)

    def render(self, document_id: str, source: Path) -> Path:
        target = self.cache_path(document_id)
        if target.exists() and target.stat().st_size > 0:
            return target
        soffice = self._soffice_path or _find_soffice()
        if not soffice:
            raise PreviewUnavailableError("libreoffice is not installed")
        with self._convert_lock:
            if target.exists() and target.stat().st_size > 0:
                return target
            workdir = Path(tempfile.mkdtemp(prefix="preview-", dir=str(self._cache)))
            profile = workdir / "profile"
            try:
                command = [
                    soffice,
                    "--headless",
                    "--norestore",
                    f"-env:UserInstallation={profile.as_uri()}",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(workdir),
                    str(source),
                ]
                self._runner(command, self._timeout)
                produced = _wait_for_pdf(workdir, source.stem, self._timeout)
                if produced is None:
                    raise PreviewUnavailableError("conversion produced no pdf")
                produced.replace(target)
            except subprocess.TimeoutExpired as exc:
                raise PreviewUnavailableError("conversion timed out") from exc
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        self.cleanup()
        return target

    def purge(self, document_id: str) -> int:
        removed = 0
        safe = _UNSAFE_ID.sub("_", document_id)
        for path in self._cache.glob(f"{safe}.*"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def cleanup(self) -> int:
        # 功能：按 mtime 从旧到新删除缓存，直到总量不超过上限。
        files = sorted(
            (p for p in self._cache.glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files)
        removed = 0
        for path in files:
            if total <= self._max_bytes:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed


def _cache_name(document_id: str) -> str:
    # 功能：把文档 id 归一成安全文件名——避免 Windows 冒号被当成数据流后缀。
    return _UNSAFE_ID.sub("_", document_id) + ".pdf"


def _default_runner(command: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(command, timeout=timeout, capture_output=True, check=False)


def _wait_for_pdf(workdir: Path, stem: str, timeout: float) -> Path | None:
    # 功能：Windows 下 soffice 异步落盘——轮询目标 PDF（或目录内任一 PDF）直到超时。
    deadline = time.time() + max(timeout, 1.0)
    expected = workdir / f"{stem}.pdf"
    while time.time() < deadline:
        if expected.exists() and expected.stat().st_size > 0:
            return expected
        candidates = [path for path in workdir.glob("*.pdf") if path.stat().st_size > 0]
        if candidates:
            return candidates[0]
        time.sleep(0.2)
    return None


def _find_soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    candidates = (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None
