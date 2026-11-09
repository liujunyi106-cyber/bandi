# 功能：TextFileProcessorTool — 文本文件处理（17 操作 + 19 转换 + 编码检测链）

from __future__ import annotations

import difflib
import importlib.util
import re
import textwrap
from typing import Any

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import _MAX_FILE_SIZE, FsTool

_MAX_CHARS = 500_000
_DETECT_BYTES = 65_536
_CHARSET_NORMALIZER_AVAILABLE = importlib.util.find_spec("charset_normalizer") is not None

_ORDERED_TEXT_ENCODINGS = (
    "utf-8",
    "utf-8-sig",
    "gb18030",
    "gbk",
    "big5",
    "shift_jis",
    "euc-jp",
    "euc-kr",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "cp1252",
    "latin-1",
)

_TRANSFORMS = (
    "uppercase",
    "lowercase",
    "title_case",
    "capitalize",
    "strip",
    "lstrip",
    "rstrip",
    "wrap",
    "indent",
    "dedent",
    "sort_lines",
    "reverse_lines",
    "unique_lines",
    "number_lines",
    "remove_blank_lines",
    "collapse_blank_lines",
    "normalize_whitespace",
    "tabs_to_spaces",
    "spaces_to_tabs",
)

_OPERATIONS = (
    "read",
    "write",
    "search",
    "replace",
    "insert",
    "append",
    "prepend",
    "transform",
    "extract",
    "split",
    "join",
    "stats",
    "head",
    "tail",
    "diff",
    "detect_encoding",
    "try_decodings",
)


class TextFileProcessorTool(FsTool):
    name: str = "text_processor"
    # 安全（③⁻）：文本文件内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Professional text-file processor: read/write with encoding auto-detection, regex "
        "search with capture groups, find-and-replace, insert/append/prepend, 19 text "
        "transforms, extract by pattern/line-range/markers, split/join files, stats, "
        "head/tail, unified diff, and encoding detection."
    )
    category: ToolCategory = ToolCategory.DOC
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"path": "file_path", "file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": list(_OPERATIONS),
                "description": "Operation to perform",
            },
            "file_path": {"type": "string", "description": "Main file path (read or write)"},
            "path": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "file_path_2": {"type": "string", "description": "Second file (diff)"},
            "encoding": {"type": "string", "description": "Codec name (default auto-detect)"},
            "encoding_2": {"type": "string", "description": "Second file encoding (diff)"},
            "data": {"type": "string", "description": "Content for write/append/prepend/insert"},
            "pattern": {"type": "string", "description": "Regex for search/replace/extract"},
            "replacement": {"type": "string", "description": "Replacement (supports \\1 backrefs)"},
            "replace_all": {"type": "boolean", "description": "Replace all matches (default true)"},
            "regex_flags": {
                "type": "string",
                "description": "i=IGNORECASE, m=MULTILINE, s=DOTALL, x=VERBOSE",
            },
            "line_number": {
                "type": "integer",
                "description": "Insert position (1-based; <=0 from end)",
            },
            "lines": {
                "type": "integer",
                "minimum": 1,
                "description": "Lines for head/tail (default 20)",
            },
            "context_lines": {
                "type": "integer",
                "minimum": 0,
                "description": "Search context lines (default 2)",
            },
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "description": "Search cap (default 100)",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500000,
                "description": "Read cap (default 500000)",
            },
            "transform_type": {
                "type": "string",
                "enum": list(_TRANSFORMS),
                "description": "Transform to apply",
            },
            "width": {
                "type": "integer",
                "minimum": 20,
                "maximum": 200,
                "description": "Wrap width (default 80)",
            },
            "indent_str": {"type": "string", "description": "Indent prefix (default 4 spaces)"},
            "tab_size": {
                "type": "integer",
                "minimum": 1,
                "maximum": 8,
                "description": "Tab size (default 4)",
            },
            "start_line": {"type": "integer", "minimum": 1, "description": "Extract start line"},
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Extract end line inclusive",
            },
            "start_marker": {"type": "string", "description": "Extract start marker (regex)"},
            "end_marker": {"type": "string", "description": "Extract end marker (regex)"},
            "delimiter": {"type": "string", "description": "Split delimiter (regex)"},
            "chunk_lines": {"type": "integer", "minimum": 1, "description": "Split by line count"},
            "output_dir": {"type": "string", "description": "Split output directory"},
            "source_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to join",
            },
            "separator": {"type": "string", "description": "Join separator (default newline)"},
            "sample_bytes": {
                "type": "integer",
                "minimum": 256,
                "maximum": 524288,
                "description": "Detect sample bytes (default 65536)",
            },
            "encodings": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Encodings to try (try_decodings)",
            },
            "sort_reverse": {"type": "boolean", "description": "Reverse sort (sort_lines)"},
            "sort_key": {
                "type": "string",
                "enum": ["alpha", "numeric", "length"],
                "description": "Sort key (sort_lines, default alpha)",
            },
        },
        "required": ["operation", "file_path"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "medium"

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "")
        dispatch = {
            "read": self._op_read,
            "write": self._op_write,
            "search": self._op_search,
            "replace": self._op_replace,
            "insert": self._op_insert,
            "append": self._op_append,
            "prepend": self._op_prepend,
            "transform": self._op_transform,
            "extract": self._op_extract,
            "split": self._op_split,
            "join": self._op_join,
            "stats": self._op_stats,
            "head": self._op_head,
            "tail": self._op_tail,
            "diff": self._op_diff,
            "detect_encoding": self._op_detect_encoding,
            "try_decodings": self._op_try_decodings,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except (ValueError, RuntimeError, OSError) as e:
            return {"success": False, "error": str(e)}

    # ── 路径与编码辅助 ───────────────────────────────────────────────────

    def _read_text(self, path, encoding: str | None = None) -> tuple[str, str, dict[str, Any]]:
        if path.stat().st_size > _MAX_FILE_SIZE:
            raise ValueError(f"file exceeds max size ({_MAX_FILE_SIZE} bytes)")
        raw = path.read_bytes()
        if self.looks_binary(raw):
            raise ValueError(f"binary file: {path.name}")
        enc = encoding or self.detect_encoding(raw)
        if enc == "latin-1":
            try:
                raw.decode("utf-8")
                enc = "utf-8"
            except UnicodeDecodeError:
                pass
        text = raw.decode(enc, errors="replace")
        return text, enc, {"used_encoding": enc, "errors": "replace"}

    @staticmethod
    def _compile_flags(flags: str) -> int:
        result = 0
        if flags:
            if "i" in flags:
                result |= re.IGNORECASE
            if "m" in flags:
                result |= re.MULTILINE
            if "s" in flags:
                result |= re.DOTALL
            if "x" in flags:
                result |= re.VERBOSE
        return result

    def _write_text(self, path, text: str, encoding: str = "utf-8") -> None:
        from bandi.tools.fs_write import atomic_write

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, text.encode(encoding))

    # ── read ─────────────────────────────────────────────────────────────

    async def _op_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        text, enc, decode = self._read_text(path, params.get("encoding"))
        max_chars = params.get("max_chars", _MAX_CHARS)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        line_endings = {
            "crlf": text.count("\r\n"),
            "lf": text.count("\n") - text.count("\r\n"),
            "cr_lone": text.count("\r") - text.count("\r\n"),
        }
        total = sum(line_endings.values()) or 1
        dominant = max(line_endings, key=line_endings.get) if total else "lf"
        return {
            "success": True,
            "text": text,
            "encoding": enc,
            "size": path.stat().st_size,
            "truncated": truncated,
            "line_endings": {**line_endings, "dominant": dominant},
            "decode": decode,
        }

    # ── write ────────────────────────────────────────────────────────────

    async def _op_write(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""), must_exist=False)
        data = params.get("data")
        if data is None:
            raise ValueError("data is required for write")
        if len(data) > _MAX_CHARS:
            raise ValueError(f"data exceeds max_write_chars {_MAX_CHARS}")
        encoding = params.get("encoding", "utf-8")
        self._write_text(path, data, encoding)
        return {
            "success": True,
            "file_path": str(path),
            "encoding": encoding,
            "chars_written": len(data),
            "size_bytes": path.stat().st_size,
        }

    # ── search ───────────────────────────────────────────────────────────

    async def _op_search(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        pattern = params.get("pattern", "")
        if not pattern:
            raise ValueError("pattern is required for search")
        text, enc, _ = self._read_text(path, params.get("encoding"))
        compiled = re.compile(pattern, self._compile_flags(params.get("regex_flags")))
        lines = text.splitlines()
        context_lines = params.get("context_lines", 2)
        max_matches = params.get("max_matches", 100)
        matches: list[dict[str, Any]] = []
        for i, line in enumerate(lines):
            m = compiled.search(line)
            if not m:
                continue
            item: dict[str, Any] = {
                "line_number": i + 1,
                "line": line,
                "context": lines[max(0, i - context_lines) : i + context_lines + 1],
                "match": m.group(0),
            }
            if m.groups():
                item["groups"] = m.groups()
            matches.append(item)
            if len(matches) >= max_matches:
                break
        return {
            "success": True,
            "file": str(path),
            "pattern": pattern,
            "encoding": enc,
            "matches": matches,
            "match_count": len(matches),
            "total_lines": len(lines),
        }

    # ── replace ──────────────────────────────────────────────────────────

    async def _op_replace(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        pattern = params.get("pattern", "")
        replacement = params.get("replacement", "")
        if not pattern:
            raise ValueError("pattern is required for replace")
        text, enc, _ = self._read_text(path, params.get("encoding"))
        compiled = re.compile(pattern, self._compile_flags(params.get("regex_flags")))
        replace_all = params.get("replace_all", True)
        new_text, count = compiled.subn(replacement, text, count=0 if replace_all else 1)
        self._write_text(path, new_text, enc)
        return {
            "success": True,
            "file_path": str(path),
            "pattern": pattern,
            "replacement": replacement,
            "replacements_made": count,
            "replace_all": replace_all,
        }

    # ── insert / append / prepend ────────────────────────────────────────

    async def _op_insert(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""), must_exist=False)
        data = params.get("data")
        if data is None:
            raise ValueError("data is required for insert")
        existing = ""
        enc = params.get("encoding") or "utf-8"
        if path.exists() and path.is_file():
            existing, enc, _ = self._read_text(path, params.get("encoding"))
        lines = existing.splitlines()
        line_number = params.get("line_number", 1)
        if line_number > 0:
            idx = min(line_number - 1, len(lines))
        else:
            idx = len(lines) + line_number
            idx = max(0, idx)
        insert_lines = data.splitlines()
        new_lines = lines[:idx] + insert_lines + lines[idx:]
        new_text = "\n".join(new_lines)
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        self._write_text(path, new_text, enc)
        return {
            "success": True,
            "file_path": str(path),
            "inserted_at_line": idx + 1,
            "lines_inserted": len(insert_lines),
            "total_lines": len(new_lines),
        }

    async def _op_append(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""), must_exist=False)
        data = params.get("data")
        if data is None:
            raise ValueError("data is required for append")
        existing = ""
        enc = params.get("encoding") or "utf-8"
        if path.exists() and path.is_file():
            existing, enc, _ = self._read_text(path, params.get("encoding"))
        separator = "\n" if existing and not existing.endswith("\n") else ""
        new_text = existing + separator + data
        self._write_text(path, new_text, enc)
        return {
            "success": True,
            "file_path": str(path),
            "appended_chars": len(data),
            "total_chars": len(new_text),
        }

    async def _op_prepend(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""), must_exist=False)
        data = params.get("data")
        if data is None:
            raise ValueError("data is required for prepend")
        existing = ""
        enc = params.get("encoding") or "utf-8"
        if path.exists() and path.is_file():
            existing, enc, _ = self._read_text(path, params.get("encoding"))
        separator = "\n" if data and not data.endswith("\n") and existing else ""
        new_text = data + separator + existing
        self._write_text(path, new_text, enc)
        return {
            "success": True,
            "file_path": str(path),
            "prepended_chars": len(data),
            "total_chars": len(new_text),
        }

    # ── transform ────────────────────────────────────────────────────────

    async def _op_transform(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        transform_type = params.get("transform_type", "")
        if transform_type not in _TRANSFORMS:
            raise ValueError(f"unknown transform_type: {transform_type}")
        text, enc, _ = self._read_text(path, params.get("encoding"))
        original = text
        if transform_type == "uppercase":
            text = text.upper()
        elif transform_type == "lowercase":
            text = text.lower()
        elif transform_type == "title_case":
            text = text.title()
        elif transform_type == "capitalize":
            text = "\n".join(line.capitalize() for line in text.splitlines())
        elif transform_type == "strip":
            text = "\n".join(line.strip() for line in text.splitlines())
        elif transform_type == "lstrip":
            text = "\n".join(line.lstrip() for line in text.splitlines())
        elif transform_type == "rstrip":
            text = "\n".join(line.rstrip() for line in text.splitlines())
        elif transform_type == "wrap":
            width = params.get("width", 80)
            text = "\n".join(
                wrapped
                for line in text.splitlines()
                for wrapped in textwrap.wrap(line, width=width)
            )
        elif transform_type == "indent":
            text = textwrap.indent(text, params.get("indent_str", "    "))
        elif transform_type == "dedent":
            text = textwrap.dedent(text)
        elif transform_type == "sort_lines":
            lines = text.splitlines()
            sort_key = params.get("sort_key", "alpha")
            reverse = params.get("sort_reverse", False)
            if sort_key == "numeric":

                def num_key(line: str):
                    m = re.search(r"-?\d+\.?\d*", line)
                    return (0, float(m.group(0))) if m else (1, 0.0)

                lines.sort(key=num_key, reverse=reverse)
            elif sort_key == "length":
                lines.sort(key=len, reverse=reverse)
            else:
                lines.sort(key=str.lower, reverse=reverse)
            text = "\n".join(lines)
        elif transform_type == "reverse_lines":
            text = "\n".join(reversed(text.splitlines()))
        elif transform_type == "unique_lines":
            seen: set[str] = set()
            out: list[str] = []
            for line in text.splitlines():
                if line not in seen:
                    seen.add(line)
                    out.append(line)
            text = "\n".join(out)
        elif transform_type == "number_lines":
            lines = text.splitlines()
            width = max(len(str(len(lines))), 1)
            text = "\n".join(f"{i + 1:>{width}} | {line}" for i, line in enumerate(lines))
        elif transform_type == "remove_blank_lines":
            text = "\n".join(line for line in text.splitlines() if line.strip())
        elif transform_type == "collapse_blank_lines":
            text = re.sub(r"\n{3,}", "\n\n", text)
        elif transform_type == "normalize_whitespace":
            text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
        elif transform_type == "tabs_to_spaces":
            tab_size = params.get("tab_size", 4)
            text = text.expandtabs(tab_size)
        elif transform_type == "spaces_to_tabs":
            tab_size = params.get("tab_size", 4)
            lines = text.splitlines()
            out_lines = []
            for line in lines:
                out = ""
                leading = len(line) - len(line.lstrip(" "))
                out += "\t" * (leading // tab_size) + " " * (leading % tab_size)
                out += line[leading:]
                out_lines.append(out)
            text = "\n".join(out_lines)
        self._write_text(path, text, enc)
        return {
            "success": True,
            "file_path": str(path),
            "transform_type": transform_type,
            "original_chars": len(original),
            "result_chars": len(text),
        }

    # ── extract ──────────────────────────────────────────────────────────

    async def _op_extract(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        text, enc, _ = self._read_text(path, params.get("encoding"))
        lines = text.splitlines()
        start_line = params.get("start_line")
        end_line = params.get("end_line")
        start_marker = params.get("start_marker")
        end_marker = params.get("end_marker")
        pattern = params.get("pattern")
        if start_line and end_line:
            extracted = lines[max(0, start_line - 1) : min(len(lines), end_line)]
            method = "line_range"
        elif start_marker or end_marker:
            start_re = re.compile(start_marker) if start_marker else None
            end_re = re.compile(end_marker) if end_marker else None
            in_range = not start_re
            extracted = []
            for line in lines:
                if start_re and start_re.search(line):
                    in_range = True
                if in_range:
                    extracted.append(line)
                if end_re and end_re.search(line):
                    break
            method = "markers"
        elif pattern:
            compiled = re.compile(pattern, self._compile_flags(params.get("regex_flags")))
            extracted = [line for line in lines if compiled.search(line)]
            method = "pattern"
        else:
            raise ValueError(
                "extract requires start_line+end_line, start_marker+end_marker, or pattern"
            )
        return {
            "success": True,
            "file": str(path),
            "method": method,
            "extracted": "\n".join(extracted),
            "line_count": len(extracted),
            "encoding": enc,
        }

    # ── split / join ─────────────────────────────────────────────────────

    async def _op_split(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        text, enc, _ = self._read_text(path, params.get("encoding"))
        lines = text.splitlines()
        chunk_lines = params.get("chunk_lines")
        delimiter = params.get("delimiter")
        if chunk_lines:
            parts = [lines[i : i + chunk_lines] for i in range(0, len(lines), chunk_lines)]
        elif delimiter:
            raw_parts = re.split(delimiter, text)
            parts = [[p] for p in raw_parts if p.strip()]
        else:
            raw_parts = re.split(r"\n---\n|\f", text)
            parts = [p.splitlines() for p in raw_parts if p.strip()]
        out_dir = params.get("output_dir")
        if out_dir:
            target_dir = self._resolve_safe_path(out_dir)
        else:
            target_dir = path.parent / f"{path.stem}_parts"
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = path.suffix or ".txt"
        files: list[str] = []
        for i, part in enumerate(parts):
            target = target_dir / f"part_{i + 1:03d}{suffix}"
            from bandi.tools.fs_write import atomic_write

            atomic_write(target, ("\n".join(part) + "\n").encode(enc))
            files.append(str(target))
        return {
            "success": True,
            "source_file": str(path),
            "output_dir": str(target_dir),
            "parts_count": len(files),
            "files": files,
        }

    async def _op_join(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""), must_exist=False)
        source_files = params.get("source_files")
        if not source_files:
            raise ValueError("source_files is required for join")
        separator = params.get("separator", "\n")
        parts: list[str] = []
        for sf in source_files:
            src = self._resolve_file(sf)
            parts.append(src.read_text(encoding="utf-8", errors="replace").rstrip("\n"))
        merged = separator.join(parts) + "\n"
        self._write_text(path, merged, params.get("encoding", "utf-8"))
        return {
            "success": True,
            "file_path": str(path),
            "files_joined": len(source_files),
            "total_chars": len(merged),
        }

    # ── stats / head / tail ──────────────────────────────────────────────

    async def _op_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        text, enc, _ = self._read_text(path, params.get("encoding"))
        lines = text.splitlines()
        words = text.split()
        line_endings = {
            "crlf": text.count("\r\n"),
            "lf": text.count("\n") - text.count("\r\n"),
            "cr_lone": text.count("\r") - text.count("\r\n"),
        }
        top_chars: dict[str, int] = {}
        for ch in text[:50_000]:
            if ch.isprintable() and not ch.isspace():
                top_chars[ch] = top_chars.get(ch, 0) + 1
        return {
            "success": True,
            "size_bytes": path.stat().st_size,
            "line_count": len(lines),
            "non_empty_lines": sum(1 for line in lines if line.strip()),
            "blank_lines": sum(1 for line in lines if not line.strip()),
            "word_count": len(words),
            "char_count": len(text),
            "avg_line_length": round(len(text) / len(lines), 1) if lines else 0.0,
            "max_line_length": max((len(line) for line in lines), default=0),
            "line_endings": line_endings,
            "top_characters": dict(
                sorted(top_chars.items(), key=lambda kv: kv[1], reverse=True)[:20]
            ),
            "encoding": enc,
        }

    async def _op_head(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        text, enc, _ = self._read_text(path, params.get("encoding"))
        lines = text.splitlines()
        count = params.get("lines", 20)
        shown = lines[:count]
        return {
            "success": True,
            "file": str(path),
            "encoding": enc,
            "lines": shown,
            "shown": len(shown),
            "total_lines": len(lines),
        }

    async def _op_tail(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        text, enc, _ = self._read_text(path, params.get("encoding"))
        lines = text.splitlines()
        count = params.get("lines", 20)
        shown = lines[-count:]
        return {
            "success": True,
            "file": str(path),
            "encoding": enc,
            "lines": shown,
            "shown": len(shown),
            "total_lines": len(lines),
        }

    # ── diff ─────────────────────────────────────────────────────────────

    async def _op_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        path2_str = params.get("file_path_2", "")
        if not path2_str:
            raise ValueError("file_path_2 is required for diff")
        path2 = self._resolve_file(path2_str)
        text1, enc1, _ = self._read_text(path, params.get("encoding"))
        text2, enc2, _ = self._read_text(path2, params.get("encoding_2") or params.get("encoding"))
        diff = list(
            difflib.unified_diff(
                text1.splitlines(),
                text2.splitlines(),
                fromfile=str(path),
                tofile=str(path2),
                lineterm="",
            )
        )
        additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        return {
            "success": True,
            "file_1": str(path),
            "file_2": str(path2),
            "encoding_1": enc1,
            "encoding_2": enc2,
            "diff": "\n".join(diff[:5000]),
            "additions": additions,
            "deletions": deletions,
            "identical": not diff,
        }

    # ── encoding ─────────────────────────────────────────────────────────

    async def _op_detect_encoding(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        sample_bytes = min(params.get("sample_bytes", _DETECT_BYTES), 524_288)
        raw = path.read_bytes()[:sample_bytes]
        nul_ratio = raw.count(b"\x00") / len(raw) if raw else 0.0
        likely_binary = nul_ratio >= 0.12
        encoding = self.detect_encoding(raw)
        candidates: list[str] = []
        for enc in _ORDERED_TEXT_ENCODINGS:
            try:
                raw.decode(enc)
                candidates.append(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        if not candidates:
            candidates = ["latin-1"]
        return {
            "success": True,
            "file": str(path),
            "sample_bytes": len(raw),
            "file_size": path.stat().st_size,
            "encoding": encoding,
            "confidence": "high" if encoding in ("utf-8", "utf-8-sig", "utf-16") else "medium",
            "likely_binary": likely_binary,
            "nul_ratio": round(nul_ratio, 4),
            "strict_decode_candidates": candidates,
            "charset_normalizer_available": _CHARSET_NORMALIZER_AVAILABLE,
        }

    async def _op_try_decodings(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""))
        encodings = params.get("encodings")
        if not encodings:
            raise ValueError("encodings is required for try_decodings")
        raw = path.read_bytes()[:_DETECT_BYTES]
        results: list[dict[str, Any]] = []
        for enc in encodings:
            try:
                decoded = raw.decode(enc)
                results.append(
                    {
                        "encoding": enc,
                        "strict_ok": True,
                        "decoded_chars": len(decoded),
                        "preview": decoded[:400] + ("..." if len(decoded) > 400 else ""),
                    }
                )
            except (UnicodeDecodeError, LookupError) as e:
                results.append(
                    {
                        "encoding": enc,
                        "strict_ok": False,
                        "error": str(e)[:200],
                        "replace_preview": raw.decode(enc, errors="replace")[:400],
                    }
                )
        return {"success": True, "file": str(path), "sample_bytes": len(raw), "results": results}
