# 功能：PDFReaderTool — PDF 处理工具（12 操作：读取/表格/图片/链接/搜索/页面信息/书签/转图/拆分/合并/提取页/元数据）

from __future__ import annotations

import re
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.doc._doc_utils import import_fitz as _import_fitz
from bandi.tools.fs_tool import FsTool

_SEARCH_MAX_MATCHES = 100

_POINTS_PER_INCH = 72


class PDFReaderTool(FsTool):
    name: str = "pdf_reader"
    # 安全（③⁻）：PDF 内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Professional PDF processor: extract text (full or by page range), extract tables, "
        "save embedded images, extract links, search text across pages, extract outline/bookmarks, "
        "convert pages to images, split/merge/extract pages, and get metadata. "
        "Operations: read, extract_tables, extract_images, extract_links, search, page_info, "
        "outline, convert_to_images, split, merge, extract_pages, metadata."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "read",
                    "extract_tables",
                    "extract_images",
                    "extract_links",
                    "search",
                    "page_info",
                    "outline",
                    "convert_to_images",
                    "split",
                    "merge",
                    "extract_pages",
                    "metadata",
                ],
                "description": "Operation to perform (default: read)",
            },
            "file_path": {"type": "string", "description": "Path to the PDF file"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "start_page": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based start page (read/extract_pages)",
            },
            "end_page": {
                "type": "integer",
                "minimum": 1,
                "description": "1-based end page inclusive (read/extract_pages)",
            },
            "pages": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Explicit page list (extract_pages), 1-based",
            },
            "query": {"type": "string", "description": "Search query (search)"},
            "case_sensitive": {
                "type": "boolean",
                "description": "Case-sensitive search (search, default false)",
            },
            "output_path": {
                "type": "string",
                "description": "Output file path (merge/extract_pages)",
            },
            "output_dir": {
                "type": "string",
                "description": "Output directory (split/convert_to_images/extract_images)",
            },
            "image_format": {
                "type": "string",
                "enum": ["png", "jpeg"],
                "description": "Image format for convert_to_images (default png)",
            },
            "dpi": {
                "type": "integer",
                "minimum": _POINTS_PER_INCH,
                "maximum": 600,
                "description": "DPI for convert_to_images (default 150)",
            },
            "merge_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to merge (merge)",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 500000,
                "description": "Max chars for read output (default 200000)",
            },
            "extract_metadata": {
                "type": "boolean",
                "description": "Include document metadata in read output (read)",
            },
        },
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation") or "read"
        dispatch = {
            "read": self._op_read,
            "extract_tables": self._op_extract_tables,
            "extract_images": self._op_extract_images,
            "extract_links": self._op_extract_links,
            "search": self._op_search,
            "page_info": self._op_page_info,
            "outline": self._op_outline,
            "convert_to_images": self._op_convert_to_images,
            "split": self._op_split,
            "merge": self._op_merge,
            "extract_pages": self._op_extract_pages,
            "metadata": self._op_metadata,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        except RuntimeError as e:
            return {"success": False, "error": str(e)}

    def _resolve_pdf(self, path_str: str):
        if not path_str:
            raise ValueError("file_path is required")
        path = self._resolve_safe_path(path_str)
        if self._is_sensitive(path):
            raise ValueError(f"sensitive path rejected: {path_str}")
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise ValueError(f"not a PDF file: {path_str}")
        return path

    def _open_doc(self, path):
        fitz = _import_fitz()
        return fitz.open(str(path))

    def _select_pages(self, params: dict[str, Any], total: int) -> list[int]:
        pages = params.get("pages")
        if pages:
            return [p for p in pages if 1 <= p <= total]
        start = max(1, params.get("start_page", 1))
        end = min(params.get("end_page", total), total)
        return list(range(start, end + 1))

    async def _op_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        max_chars = params.get("max_chars", 200_000)
        with self._open_doc(path) as doc:
            total = len(doc)
            pages_sel = self._select_pages(params, total)
            page_texts: list[dict[str, Any]] = []
            full_parts: list[str] = []
            empty_pages = 0
            for p in pages_sel:
                text = doc[p - 1].get_text("text")
                if not text.strip():
                    empty_pages += 1
                page_texts.append({"page_number": p, "char_count": len(text)})
                full_parts.append(text)
            content = "\n\n".join(full_parts)
            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]
            result: dict[str, Any] = {
                "success": True,
                "format": "pdf",
                "text": content,
                "pages": page_texts,
                "total_pages": total,
                "extracted_pages": len(pages_sel),
                "total_characters": len(content),
                "truncated": truncated,
            }
            if empty_pages:
                result["warning"] = f"{empty_pages} page(s) had no extractable text"
            if params.get("extract_metadata"):
                result["metadata"] = doc.metadata
            return result

    async def _op_extract_tables(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        with self._open_doc(path) as doc:
            tables: list[dict[str, Any]] = []
            pages_scanned = 0
            for p in range(len(doc)):
                page = doc[p]
                pages_scanned += 1
                try:
                    found = page.find_tables()
                    for tab in found.tables:
                        extracted = tab.extract()
                        headers = extracted[0] if extracted else []
                        rows = extracted[1:] if extracted else []
                        tables.append(
                            {
                                "page": p + 1,
                                "table_index": len([t for t in tables if t["page"] == p + 1]),
                                "headers": headers,
                                "rows": rows,
                                "row_count": len(rows),
                                "col_count": len(headers),
                            }
                        )
                except AttributeError:
                    for line in page.get_text("text").splitlines():
                        if line.count("\t") >= 2 or len(re.findall(r"\s{2,}", line)) >= 2:
                            cells = re.split(r"\t+|\s{2,}", line.strip())
                            tables.append(
                                {
                                    "page": p + 1,
                                    "table_index": len([t for t in tables if t["page"] == p + 1]),
                                    "headers": [],
                                    "rows": [cells],
                                    "row_count": 1,
                                    "col_count": len(cells),
                                    "detection": "heuristic",
                                }
                            )
        return {
            "success": True,
            "file": str(path),
            "tables": tables,
            "table_count": len(tables),
            "pages_scanned": pages_scanned,
        }

    async def _op_extract_images(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        fitz = _import_fitz()
        out_dir = self._resolve_output_dir(params, f"{path.stem}_images")
        images: list[dict[str, Any]] = []
        with self._open_doc(path) as doc:
            pages_scanned = 0
            for p in range(len(doc)):
                page = doc[p]
                pages_scanned += 1
                for img_index, img in enumerate(page.get_images(full=True)):
                    xref = img[0]
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha > 3:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    target = out_dir / f"page{p + 1}_img{img_index}.png"
                    pix.save(str(target))
                    images.append(
                        {
                            "page": p + 1,
                            "image_index": img_index,
                            "path": str(target),
                            "width": pix.width,
                            "height": pix.height,
                            "colorspace": pix.colorspace.name if pix.colorspace else None,
                        }
                    )
        return {
            "success": True,
            "file": str(path),
            "output_dir": str(out_dir),
            "images": images,
            "image_count": len(images),
            "pages_scanned": pages_scanned,
        }

    async def _op_extract_links(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        with self._open_doc(path) as doc:
            links: list[dict[str, Any]] = []
            external = 0
            internal = 0
            for p in range(len(doc)):
                for link in doc[p].get_links():
                    item: dict[str, Any] = {"page": p + 1}
                    if "uri" in link:
                        item["uri"] = link["uri"]
                        item["type"] = "external"
                        external += 1
                    elif "page" in link:
                        item["target_page"] = link["page"] + 1
                        item["type"] = "internal"
                        internal += 1
                    else:
                        item["type"] = "other"
                    links.append(item)
        return {
            "success": True,
            "file": str(path),
            "links": links,
            "link_count": len(links),
            "external_links": external,
            "internal_links": internal,
            "pages_scanned": len({link["page"] for link in links}) or 0,
        }

    async def _op_search(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        query = params.get("query", "")
        if not query:
            raise ValueError("query is required for search")
        flags = 0 if params.get("case_sensitive") else re.IGNORECASE
        pattern = re.compile(re.escape(query), flags)
        with self._open_doc(path) as doc:
            results: list[dict[str, Any]] = []
            pages_scanned = 0
            for p in range(len(doc)):
                page = doc[p]
                pages_scanned += 1
                lines = page.get_text("text").splitlines()
                for i, line in enumerate(lines):
                    if pattern.search(line):
                        results.append(
                            {
                                "page": p + 1,
                                "line_number": i + 1,
                                "line": line,
                                "context": [ctx_line for ctx_line in lines[max(0, i - 1) : i + 2]],
                            }
                        )
                        if len(results) >= _SEARCH_MAX_MATCHES:
                            return {
                                "success": True,
                                "file": str(path),
                                "query": query,
                                "case_sensitive": bool(params.get("case_sensitive")),
                                "results": results,
                                "match_count": len(results),
                                "pages_scanned": pages_scanned,
                                "truncated": True,
                            }
        return {
            "success": True,
            "file": str(path),
            "query": query,
            "case_sensitive": bool(params.get("case_sensitive")),
            "results": results,
            "match_count": len(results),
            "pages_scanned": pages_scanned,
            "truncated": False,
        }

    async def _op_page_info(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        with self._open_doc(path) as doc:
            pages: list[dict[str, Any]] = []
            for p in range(len(doc)):
                page = doc[p]
                rect = page.rect
                text = page.get_text("text")
                images = page.get_images(full=True)
                if not text.strip() and not images:
                    content_type = "empty"
                elif text.strip() and not images:
                    content_type = "text"
                elif not text.strip() and images:
                    content_type = "image_only"
                else:
                    content_type = "mixed"
                pages.append(
                    {
                        "page_number": p + 1,
                        "width": round(rect.width, 2),
                        "height": round(rect.height, 2),
                        "width_inches": round(rect.width / _POINTS_PER_INCH, 2),
                        "height_inches": round(rect.height / _POINTS_PER_INCH, 2),
                        "rotation": page.rotation,
                        "content_type": content_type,
                        "char_count": len(text),
                        "image_count": len(images),
                    }
                )
        return {
            "success": True,
            "file": str(path),
            "total_pages": len(pages),
            "pages": pages,
        }

    async def _op_outline(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        with self._open_doc(path) as doc:
            bookmarks = []
            for item in doc.get_toc(simple=False):
                lvl, title, page = item[0], item[1], item[2]
                if title.strip():
                    bookmarks.append({"level": lvl, "title": title, "page": page})
            total_pages = len(doc)
        return {
            "success": True,
            "file": str(path),
            "bookmarks": bookmarks,
            "bookmark_count": len(bookmarks),
            "has_outline": len(bookmarks) > 0,
            "total_pages": total_pages,
        }

    def _resolve_output_dir(self, params: dict[str, Any], default_name: str):
        out = params.get("output_dir")
        if out:
            target = self._resolve_safe_path(out)
            if self._is_sensitive(target):
                raise ValueError(f"sensitive path rejected: {out}")
        else:
            parent = self._resolve_safe_path(params.get("file_path", "")).parent
            target = parent / default_name
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def _op_convert_to_images(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        fitz = _import_fitz()
        fmt = params.get("image_format", "png")
        dpi = params.get("dpi", 150)
        out_dir = self._resolve_output_dir(params, f"{path.stem}_pages")
        zoom = dpi / _POINTS_PER_INCH
        matrix = fitz.Matrix(zoom, zoom)
        files: list[dict[str, Any]] = []
        with self._open_doc(path) as doc:
            for p in range(len(doc)):
                pix = doc[p].get_pixmap(matrix=matrix)
                target = out_dir / f"page_{p + 1:03d}.{fmt}"
                pix.save(str(target), output=fmt)
                files.append(
                    {
                        "page": p + 1,
                        "path": str(target),
                        "width": pix.width,
                        "height": pix.height,
                    }
                )
        return {
            "success": True,
            "file": str(path),
            "output_dir": str(out_dir),
            "format": fmt,
            "dpi": dpi,
            "files": files,
            "page_count": len(files),
        }

    async def _op_split(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        fitz = _import_fitz()
        out_dir = self._resolve_output_dir(params, f"{path.stem}_split")
        files: list[str] = []
        with self._open_doc(path) as doc:
            for p in range(len(doc)):
                out = fitz.open()
                out.insert_pdf(doc, from_page=p, to_page=p)
                target = out_dir / f"page_{p + 1:03d}.pdf"
                out.save(str(target))
                out.close()
                files.append(str(target))
        return {
            "success": True,
            "file": str(path),
            "output_dir": str(out_dir),
            "files": files,
            "page_count": len(files),
        }

    async def _op_merge(self, params: dict[str, Any]) -> dict[str, Any]:
        merge_files = params.get("merge_files")
        if not merge_files:
            raise ValueError("merge_files is required for merge")
        fitz = _import_fitz()
        sources = [self._resolve_pdf(f) for f in merge_files]
        merged = fitz.open()
        for src in sources:
            with self._open_doc(src) as doc:
                merged.insert_pdf(doc)
        if params.get("output_path"):
            target = self._resolve_safe_path(params["output_path"])
            if self._is_sensitive(target):
                raise ValueError(f"sensitive path rejected: {params['output_path']}")
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target = sources[0].parent / "merged.pdf"
        total_pages = len(merged)
        merged.save(str(target))
        merged.close()
        return {
            "success": True,
            "output_path": str(target),
            "files_merged": len(sources),
            "total_pages": total_pages,
            "size_bytes": target.stat().st_size,
        }

    async def _op_extract_pages(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        fitz = _import_fitz()
        with self._open_doc(path) as doc:
            total = len(doc)
            pages_sel = self._select_pages(params, total)
            if not pages_sel:
                raise ValueError("no valid pages selected")
            out = fitz.open()
            for p in pages_sel:
                out.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
            out_path = params.get("output_path")
            if out_path:
                target = self._resolve_safe_path(out_path)
                if self._is_sensitive(target):
                    raise ValueError(f"sensitive path rejected: {out_path}")
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                target = path.parent / f"{path.stem}_extracted.pdf"
            out.save(str(target))
            out.close()
        return {
            "success": True,
            "output_path": str(target),
            "pages_extracted": len(pages_sel),
            "source_total_pages": total,
            "size_bytes": target.stat().st_size,
        }

    async def _op_metadata(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_pdf(params.get("file_path", ""))
        with self._open_doc(path) as doc:
            metadata = dict(doc.metadata)
            total_chars = sum(len(doc[p].get_text("text")) for p in range(len(doc)))
            total_pages = len(doc)
        metadata.update(
            {
                "success": True,
                "file_path": str(path),
                "file_size_bytes": path.stat().st_size,
                "total_pages": total_pages,
                "total_characters": total_chars,
            }
        )
        return metadata
