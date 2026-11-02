# 功能：WordReaderTool — Word 文档读取（.docx/.docm 结构提取 + .doc 遗留格式回退）

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from bandi.tools.base import ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool


class WordReaderTool(FsTool):
    name: str = "word_reader"
    # 安全（③⁻）：Word 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Extract text from Word documents: .docx/.docm (paragraphs, headings, tables, "
        "header/footer via python-docx) or legacy binary .doc (plain text via pyantiword "
        "or LibreOffice fallback)."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the Word document (.docx, .docm, or legacy .doc)",
            },
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "extract_tables": {
                "type": "boolean",
                "description": "Whether to extract tables (.docx/.docm only). Default true",
            },
            "extract_headers": {
                "type": "boolean",
                "description": "Whether to extract header/footer content (.docx/.docm only). Default false",
            },
            "preserve_structure": {
                "type": "boolean",
                "description": "Whether to preserve structure (headings/tables as typed items). Default true",
            },
        },
        "required": ["file_path"],
    }
    is_concurrency_safe: bool = True
    is_read_only: bool = True
    risk_level: str = "low"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        path_str = params.get("file_path", "")
        if not path_str:
            return {"success": False, "error": "file_path is required"}
        try:
            path = self._resolve_safe_path(path_str)
            if self._is_sensitive(path):
                return {"success": False, "error": f"sensitive path rejected: {path_str}"}
            if not path.is_file():
                return {"success": False, "error": f"file not found: {path_str}"}
            suffix = path.suffix.lower()
            if suffix == ".doc":
                # 是同步阻塞 IO——移入线程池，防阻塞事件循环
                return await asyncio.to_thread(self._read_legacy_doc, path)
            if suffix in (".docx", ".docm"):
                return await asyncio.to_thread(
                    self._read_docx,
                    path,
                    extract_tables=params.get("extract_tables", True),
                    extract_headers=params.get("extract_headers", False),
                    preserve_structure=params.get("preserve_structure", True),
                )
            return {"success": False, "error": f"unsupported Word format: {suffix}"}
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _read_docx(
        self, path, *, extract_tables: bool, extract_headers: bool, preserve_structure: bool
    ) -> dict[str, Any]:
        try:
            from docx import Document
            from docx.opc.exceptions import PackageNotFoundError
        except ImportError:
            raise RuntimeError("python-docx is required for .docx reading; install python-docx")
        try:
            doc = Document(str(path))
        except PackageNotFoundError:
            raise ValueError(f"invalid or corrupted docx: {path}")
        content: list[dict[str, Any]] = []
        full_text_parts: list[str] = []
        heading_count = 0
        table_count = 0
        paragraph_count = 0

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style_name = getattr(para.style, "name", "") or ""
            if style_name.startswith("Heading"):
                heading_count += 1
                level = 1
                try:
                    level = int(style_name.replace("Heading ", ""))
                except ValueError:
                    level = 1
                if preserve_structure:
                    content.append({"type": "heading", "text": text, "level": level})
                full_text_parts.append(text)
            else:
                paragraph_count += 1
                if preserve_structure:
                    content.append({"type": "paragraph", "text": text})
                full_text_parts.append(text)

        tables: list[list[list[str]]] = []
        if extract_tables:
            for table in doc.tables:
                rows: list[list[str]] = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    rows.append(cells)
                    full_text_parts.append(" | ".join(cells))
                tables.append(rows)
                table_count += 1
                if preserve_structure:
                    content.append(
                        {
                            "type": "table",
                            "rows": rows,
                            "row_count": len(rows),
                            "col_count": len(rows[0]) if rows else 0,
                        }
                    )

        headers_footers: dict[str, list[str]] = {}
        if extract_headers:
            headers: list[str] = []
            footers: list[str] = []
            for section in doc.sections:
                headers.extend(p.text.strip() for p in section.header.paragraphs if p.text.strip())
                footers.extend(p.text.strip() for p in section.footer.paragraphs if p.text.strip())
            headers_footers = {"headers": headers, "footers": footers}

        cp = doc.core_properties
        metadata = {
            "title": cp.title or "",
            "author": cp.author or "",
            "subject": cp.subject or "",
            "keywords": cp.keywords or "",
            "created": str(cp.created) if cp.created else "",
            "modified": str(cp.modified) if cp.modified else "",
            "last_modified_by": cp.last_modified_by or "",
        }
        text = "\n\n".join(full_text_parts)
        result: dict[str, Any] = {
            "success": True,
            "text": text,
            "total_characters": len(text),
            "paragraph_count": paragraph_count,
            "heading_count": heading_count,
            "table_count": table_count,
            "metadata": metadata,
        }
        if preserve_structure and content:
            result["content"] = content
        if extract_tables and tables:
            result["tables"] = tables
        if extract_headers:
            result["headers_footers"] = headers_footers
        return result

    def _read_legacy_doc(self, path) -> dict[str, Any]:
        text = self._extract_legacy_doc_text(path)
        if not text:
            return {"success": False, "error": "could not extract text from legacy .doc"}
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        content = [{"type": "paragraph", "text": p.strip()} for p in paragraphs]
        return {
            "success": True,
            "text": text.strip(),
            "total_characters": len(text),
            "paragraph_count": len(paragraphs),
            "heading_count": 0,
            "table_count": 0,
            "metadata": {"legacy_format": "doc", "extraction_source": self._legacy_source},
            "content": content,
            "note": "Legacy .doc: table structure is not extracted; plain text only.",
        }

    _legacy_source: str = ""

    def _extract_legacy_doc_text(self, path) -> str:
        try:
            from pyantiword.antiword_wrapper import extract_text_with_antiword

            self._legacy_source = "pyantiword"
            return extract_text_with_antiword(str(path))
        except ImportError:
            pass
        try:
            import shutil
            import subprocess
            import tempfile

            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            if not soffice:
                program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
                soffice_candidates = (
                    Path(program_files) / "LibreOffice" / "program" / "soffice.exe",
                    Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
                )
                for candidate in soffice_candidates:
                    if candidate.is_file():
                        soffice = str(candidate)
                        break
            if not soffice:
                raise RuntimeError(
                    "legacy .doc requires pyantiword or LibreOffice (soffice) installed"
                )
            with tempfile.TemporaryDirectory() as tmp:
                profile = Path(tempfile.gettempdir()) / "bandi_lo_profile"
                profile.mkdir(exist_ok=True)
                subprocess.run(
                    [
                        soffice,
                        "--headless",
                        f"-env:UserInstallation=file:///{profile.as_posix()}",
                        "--convert-to",
                        "txt:Text",
                        str(path),
                        "--outdir",
                        tmp,
                    ],
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                out = Path(tmp) / f"{path.stem}.txt"
                if out.exists():
                    self._legacy_source = "libreoffice"
                    return out.read_text(encoding="utf-8", errors="replace")
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
        raise RuntimeError("legacy .doc requires pyantiword or LibreOffice (soffice) installed")
