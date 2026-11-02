# 功能：ConfigFileTool — 结构化配置文件（JSON/YAML/TOML）read/write/query/merge/convert

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

try:
    import tomllib
except ImportError:  # pragma: no cover - py3.11+ 自带
    import tomli as tomllib  # type: ignore[no-redef, import-not-found]

from bandi.tools.base import RetryPolicy, ToolCategory, ToolPermissionContext
from bandi.tools.fs_tool import FsTool

_FORMATS = ("json", "yaml", "toml")


def _detect_format_from_path(path: Path) -> str | None:
    suf = path.suffix.lower()
    if suf == ".json":
        return "json"
    if suf in (".yaml", ".yml"):
        return "yaml"
    if suf == ".toml":
        return "toml"
    return None


def _try_parse(text: str, fmt: str) -> object:
    if fmt == "json":
        return json.loads(text)
    if fmt == "yaml":
        return yaml.safe_load(text)
    if fmt == "toml":
        return tomllib.loads(text)
    raise ValueError(f"unknown format: {fmt}")


def _parse_with_fallback(text: str, hint: str | None) -> tuple[object, str]:
    if hint:
        try:
            return _try_parse(text, hint), hint
        except (ValueError, yaml.YAMLError):
            pass
    last_err: Exception | None = None
    for fmt in _FORMATS:
        if fmt == hint:
            continue
        try:
            return _try_parse(text, fmt), fmt
        except (ValueError, yaml.YAMLError) as e:
            last_err = e
    raise ValueError(f"could not parse config (tried {_FORMATS}): {last_err}")


def _recursive_dict_key_count(obj: Any) -> int:
    n = 0
    if isinstance(obj, dict):
        n += len(obj)
        for v in obj.values():
            n += _recursive_dict_key_count(v)
    elif isinstance(obj, list):
        for item in obj:
            n += _recursive_dict_key_count(item)
    return n


def _deep_merge(base: object, overlay: object) -> object:
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = _deep_merge(out[k], v) if k in out else v
        return out
    return overlay


def _get_by_path(data: object, path: str) -> object | None:
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _manual_toml_dump(obj: dict[str, Any]) -> str:
    lines: list[str] = []

    def esc_key(k: str) -> str:
        if k and all(c.isalnum() or c in "_-" for c in k) and not k[0].isdigit():
            return k
        return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def fmt_scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        if isinstance(v, list):
            if not v:
                return "[]"
            if all(isinstance(x, (str, int, float, bool)) or x is None for x in v):
                return "[" + ", ".join(fmt_scalar(x) for x in v) + "]"
            raise ValueError("TOML manual dump: only lists of scalars are supported")
        raise TypeError(f"TOML manual dump: unsupported type {type(v).__name__}")

    def walk(prefix: tuple[str, ...], d: dict[str, Any]) -> None:
        scalars: dict[str, Any] = {}
        children: dict[str, dict[str, Any]] = {}
        for k, v in d.items():
            if isinstance(v, dict):
                children[k] = v
            else:
                scalars[k] = v
        if prefix:
            lines.append(f"[{'.'.join(esc_key(x) for x in prefix)}]")
        for k in sorted(scalars):
            lines.append(f"{esc_key(k)} = {fmt_scalar(scalars[k])}")
        for k in sorted(children):
            walk(prefix + (k,), children[k])

    walk((), obj)
    return "\n".join(lines) + ("\n" if lines else "")


def _dump_toml(data: dict[str, Any]) -> str:
    try:
        import tomli_w

        return tomli_w.dumps(data)
    except ImportError:
        return _manual_toml_dump(data)


def _serialize(data: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
    if fmt == "yaml":
        return yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    if fmt == "toml":
        if not isinstance(data, dict):
            raise ValueError("TOML output requires a mapping at the root")
        return _dump_toml(data)
    raise ValueError(f"unknown format: {fmt}")


class ConfigFileTool(FsTool):
    name: str = "config_file"
    # 安全（③⁻）：配置文件内容外部携带——输出按不可信外部内容包裹（executor 6.14）
    external_content: bool = True
    description: str = (
        "Read, write, query, merge, and convert structured config files "
        "(JSON, YAML, TOML). Operations: read (parsed data + key count), write (serialize "
        "object to file), query (dot path lookup like database.host or servers.0.name), "
        "merge (deep-merge base + overlay), convert (serialize to another format)."
    )
    category: ToolCategory = ToolCategory.DOC
    # schema 面同步声明别名属性（否则模型看不到 / 提供商严格校验会拒）
    param_aliases: dict[str, str] = {"file": "file_path"}
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["read", "write", "query", "merge", "convert"],
                "description": "Config file operation",
            },
            "file_path": {
                "type": "string",
                "description": "Config file path (existing for read/query/convert; destination for write)",
            },
            "file": {
                "type": "string",
                "description": "Alias of 'file_path' — accepted interchangeably",
            },
            "data": {"type": "object", "description": "Object to write (write operation)"},
            "format": {
                "type": "string",
                "enum": ["json", "yaml", "toml"],
                "description": "Serialization format (optional; inferred from extension)",
            },
            "path": {"type": "string", "description": "Dot path for query (e.g. database.host)"},
            "base_path": {"type": "string", "description": "Base config file path (merge)"},
            "overlay_path": {"type": "string", "description": "Overlay config file path (merge)"},
            "output_path": {"type": "string", "description": "Optional output file path"},
            "output_format": {
                "type": "string",
                "enum": ["json", "yaml", "toml"],
                "description": "Target format for convert",
            },
        },
        "required": ["operation"],
    }
    is_concurrency_safe: bool = False
    risk_level: str = "low"
    retry_policy: RetryPolicy = RetryPolicy(retryable=False)

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        operation = params.get("operation", "")
        dispatch = {
            "read": self._op_read,
            "write": self._op_write,
            "query": self._op_query,
            "merge": self._op_merge,
            "convert": self._op_convert,
        }
        if operation not in dispatch:
            return {"success": False, "error": f"unknown operation: {operation}"}
        try:
            return await dispatch[operation](params)
        except (ValueError, OSError) as e:
            return {"success": False, "error": str(e)}

    def _load_parsed(self, path: Path) -> tuple[Any, str]:
        text = path.read_text(encoding="utf-8")
        return _parse_with_fallback(text, _detect_format_from_path(path))

    async def _op_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = self._resolve_file(params.get("file_path", ""), must_exist=True)
        data, fmt = self._load_parsed(path)
        return {
            "success": True,
            "file": str(path),
            "data": data,
            "format": fmt,
            "key_count": _recursive_dict_key_count(data),
        }

    async def _op_write(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        if data is None:
            raise ValueError("data is required for write")
        path = self._resolve_file(params.get("file_path", ""), must_exist=False)
        fmt = params.get("format") or _detect_format_from_path(path)
        if not fmt:
            raise ValueError("format is required when extension does not imply json, yaml, or toml")
        path.parent.mkdir(parents=True, exist_ok=True)
        from bandi.tools.fs_write import atomic_write

        atomic_write(path, _serialize(data, fmt).encode("utf-8"))
        return {
            "success": True,
            "file": str(path),
            "format": fmt,
            "size_bytes": path.stat().st_size,
        }

    async def _op_query(self, params: dict[str, Any]) -> dict[str, Any]:
        qpath = params.get("path")
        if qpath is None or qpath == "":
            raise ValueError("path is required for query")
        path = self._resolve_file(params.get("file_path", ""), must_exist=True)
        data, _ = self._load_parsed(path)
        return {"success": True, "file": str(path), "value": _get_by_path(data, qpath)}

    async def _op_merge(self, params: dict[str, Any]) -> dict[str, Any]:
        base_path = params.get("base_path", "")
        overlay_path = params.get("overlay_path", "")
        if not base_path or not overlay_path:
            raise ValueError("base_path and overlay_path are required for merge")
        base = self._resolve_file(base_path, must_exist=True)
        overlay = self._resolve_file(overlay_path, must_exist=True)
        merged = _deep_merge(self._load_parsed(base)[0], self._load_parsed(overlay)[0])
        result: dict[str, Any] = {"success": True, "data": merged}
        out = params.get("output_path")
        if out:
            opath = self._resolve_file(out, must_exist=False)
            fmt = _detect_format_from_path(opath) or "json"
            opath.parent.mkdir(parents=True, exist_ok=True)
            from bandi.tools.fs_write import atomic_write

            atomic_write(opath, _serialize(merged, fmt).encode("utf-8"))
            result["output_path"] = str(opath)
        return result

    async def _op_convert(self, params: dict[str, Any]) -> dict[str, Any]:
        output_format = params.get("output_format", "")
        if output_format not in _FORMATS:
            raise ValueError(f"output_format must be one of {_FORMATS}")
        path = self._resolve_file(params.get("file_path", ""), must_exist=True)
        data, _ = self._load_parsed(path)
        content = _serialize(data, output_format)
        result: dict[str, Any] = {
            "success": True,
            "file": str(path),
            "content": content,
            "output_format": output_format,
        }
        outp = params.get("output_path")
        if outp:
            opath = self._resolve_file(outp, must_exist=False)
            opath.parent.mkdir(parents=True, exist_ok=True)
            opath.write_text(content, encoding="utf-8", newline="")
            result["output_path"] = str(opath)
        return result
