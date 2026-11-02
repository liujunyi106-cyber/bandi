# 功能：json_parser — JSON 解析/查询/校验/合并/差异/展平/序列化（基准 LeAgent tools/util/json_parser.py 移植）
from __future__ import annotations

import copy
import json
import re
from typing import Any

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext


def _split_key(key: str) -> tuple[str | int, ...] | None:
    """功能：解析展平键 `a.b[0]` → ('a','b',0)；非展平键返回 None。"""
    parts: list[str | int] = []
    for token in key.split("."):
        token = token.strip()
        if not token:
            return None
        m = re.match(r"^([^\[]+)(.*)$", token)
        name = m.group(1) if m else token
        parts.append(name)
        rest = m.group(2) if m else ""
        for idx in re.finditer(r"\[(\d+)\]", rest):
            parts.append(int(idx.group(1)))
    return tuple(parts)


class JsonParserTool(BaseTool):
    """功能：JSON 解析/查询/校验/转换/合并/差异/展平/序列化/提取（10 个 op）。"""

    name = "json_parser"
    description = (
        "JSON 处理：parse/query/validate/transform/merge/diff/flatten/unflatten/stringify/extract"
    )
    category = ToolCategory.UTIL
    feature_group = "util"
    is_read_only = True
    is_concurrency_safe = True
    capability_tags: list[str] = []
    parameters = {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "parse",
                    "query",
                    "validate",
                    "transform",
                    "merge",
                    "diff",
                    "flatten",
                    "unflatten",
                    "stringify",
                    "extract",
                ],
                "description": "要执行的操作",
            },
            "data": {},
            "data2": {},
            "text": {"type": "string"},
            "path": {"type": "string", "description": "query 路径"},
            "schema": {},
            "transform_spec": {},
            "merge_strategy": {"type": "string", "enum": ["deep", "shallow", "replace"]},
            "separator": {"type": "string", "default": "."},
            "indent": {"type": "integer", "default": 2},
            "strict": {"type": "boolean", "description": "parse 严格失败即抛错"},
        },
        "required": ["operation"],
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        op = params.get("operation", "parse")
        handler = getattr(self, f"_op_{op}", None)
        if handler is None:
            raise ValueError(f"Unknown operation: {op}")
        return handler(params)

    def _op_parse(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text", "")
        strict = params.get("strict", False)
        if not text:
            if params.get("data") is not None:
                return {"data": params["data"], "source": "provided"}
            raise ValueError("Text is required for parse operation")
        text = text.strip()
        try:
            data = json.loads(text)
            return {"data": data, "valid": True, "type": type(data).__name__}
        except json.JSONDecodeError as e:
            if strict:
                raise
            extracted = self._try_extract_json(text)
            if extracted is not None:
                return {
                    "data": extracted,
                    "valid": True,
                    "extracted": True,
                    "type": type(extracted).__name__,
                }
            return {"data": None, "valid": False, "error": str(e), "error_position": e.pos}

    def _try_extract_json(self, text: str) -> Any | None:
        code_block = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if code_block:
            try:
                return json.loads(code_block.group(1).strip())
            except json.JSONDecodeError:
                pass
        for start, end in [("{", "}"), ("[", "]")]:
            first = text.find(start)
            if first != -1:
                depth = 0
                for i, char in enumerate(text[first:], first):
                    if char == start:
                        depth += 1
                    elif char == end:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[first : i + 1])
                            except json.JSONDecodeError:
                                break
        return None

    def _op_query(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        path = params.get("path")
        if data is None:
            if params.get("text"):
                data = self._parse_json_text(params["text"])
            else:
                raise ValueError("Data or text is required for query operation")
        if not path:
            return {"found": True, "data": data}
        keys = [k for k in path.split(".") if k] if "." in path else [path]
        current: Any = data
        found = True
        for k in keys:
            m = re.match(r"^([^\[]+)(.*)$", k)
            name = m.group(1) if m else k
            if isinstance(current, dict) and name in current:
                current = current[name]
            else:
                found = False
                break
            for idx in re.finditer(r"\[(\d+)\]", (m.group(2) if m else "")):
                i = int(idx.group(1))
                if isinstance(current, list) and i < len(current):
                    current = current[i]
                else:
                    found = False
                    break
            if not found:
                break
        return {"found": found, "data": current if found else None, "path": path}

    def _parse_json_text(self, text: str) -> Any:
        return json.loads(text.strip())

    def _op_validate(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        schema = params.get("schema")
        if data is None:
            if params.get("text"):
                data = self._parse_json_text(params["text"])
        if schema is None:
            raise ValueError("Schema is required for validate operation")
        import jsonschema

        try:
            jsonschema.validate(instance=data, schema=schema)
            return {"valid": True, "data": data, "errors": []}
        except jsonschema.ValidationError as e:
            return {"valid": False, "data": data, "errors": [e.message]}

    def _op_transform(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        spec = params.get("transform_spec", {})
        if data is None:
            raise ValueError("Data is required for transform operation")
        if not spec:
            return {"data": data, "transformed": False}
        result: dict[str, Any] = {}
        for target_field, source_spec in spec.items():
            if isinstance(source_spec, str):
                mapping = {"source": source_spec}
            else:
                mapping = dict(source_spec)
            source_path = mapping.get("source", "")
            value = self._get_path(data, source_path)
            if callable_transform := _APPLY_TRANSFORMS.get(str(mapping.get("apply", "identity"))):
                value = callable_transform(value)
            self._set_path(result, target_field, value)
        return {"data": result, "transformed": True}

    def _get_path(self, data: Any, path: str) -> Any:
        if not path:
            return data
        current = data
        for part in path.split("."):
            m = re.match(r"^([^\[]+)(.*)$", part)
            name = m.group(1) if m else part
            if isinstance(current, dict) and name in current:
                current = current[name]
            else:
                return None
            rest = m.group(2) if m else ""
            for idx in re.finditer(r"\[(\d+)\]", rest):
                i = int(idx.group(1))
                if isinstance(current, list) and i < len(current):
                    current = current[i]
                else:
                    return None
        return current

    def _set_path(self, data: dict[str, Any], path: str, value: Any) -> None:
        keys = _split_key(path)
        if not keys:
            return
        current: Any = data
        for i, k in enumerate(keys[:-1]):
            if isinstance(k, int):
                if not isinstance(current, list):
                    return
                while len(current) <= k:
                    current.append({})
                current = current[k]
            else:
                nxt = current.get(k) if isinstance(current, dict) else None
                child_is_index = isinstance(keys[i + 1], int)
                if not isinstance(nxt, (dict, list)):
                    nxt = [] if child_is_index else {}
                    if isinstance(current, dict):
                        current[k] = nxt
                current = nxt
        last = keys[-1]
        if isinstance(last, int):
            if isinstance(current, list):
                while len(current) <= last:
                    current.append(None)
                current[last] = value
        elif isinstance(current, dict):
            current[last] = value

    def _op_merge(self, params: dict[str, Any]) -> dict[str, Any]:
        data1 = params.get("data")
        data2 = params.get("data2")
        strategy = params.get("merge_strategy", "deep")
        if data1 is None or data2 is None:
            raise ValueError("Both data and data2 are required for merge operation")
        if strategy == "replace":
            return {"data": data2}
        if strategy == "shallow":
            if isinstance(data1, dict) and isinstance(data2, dict):
                merged = dict(data1)
                merged.update(data2)
                return {"data": merged}
            return {"data": data2}
        return {"data": self._deep_merge(copy.deepcopy(data1), data2), "strategy": "deep"}

    def _deep_merge(self, base: Any, override: Any) -> Any:
        if isinstance(base, dict) and isinstance(override, dict):
            for k, v in override.items():
                if k in base:
                    base[k] = self._deep_merge(base[k], v)
                else:
                    base[k] = copy.deepcopy(v)
            return base
        if isinstance(base, list) and isinstance(override, list):
            return base + copy.deepcopy(override)
        return copy.deepcopy(override)

    def _op_diff(self, params: dict[str, Any]) -> dict[str, Any]:
        data1 = params.get("data")
        data2 = params.get("data2")
        if data1 is None or data2 is None:
            raise ValueError("Both data and data2 are required for diff operation")
        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []

        def compare(path: str, v1: Any, v2: Any) -> None:
            if _type_mismatch(v1, v2):
                changed.append({"path": path, "old": v1, "new": v2})
            elif isinstance(v1, dict):
                all_keys = set(v1.keys()) | set(v2.keys())
                for key in all_keys:
                    new_path = f"{path}.{key}" if path else key
                    if key not in v1:
                        added.append({"path": new_path, "value": v2[key]})
                    elif key not in v2:
                        removed.append({"path": new_path, "value": v1[key]})
                    else:
                        compare(new_path, v1[key], v2[key])
            elif isinstance(v1, list):
                if len(v1) != len(v2):
                    changed.append({"path": path, "old": v1, "new": v2})
                else:
                    for i, (item1, item2) in enumerate(zip(v1, v2)):
                        compare(f"{path}[{i}]", item1, item2)
            elif v1 != v2:
                changed.append({"path": path, "old": v1, "new": v2})

        compare("", data1, data2)
        return {
            "added": added,
            "removed": removed,
            "changed": changed,
            "has_differences": bool(added or removed or changed),
            "summary": {
                "added_count": len(added),
                "removed_count": len(removed),
                "changed_count": len(changed),
            },
        }

    def _op_flatten(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        separator = params.get("separator", ".")
        if data is None:
            raise ValueError("Data is required for flatten operation")
        result: dict[str, Any] = {}

        def flatten(obj: Any, prefix: str = "") -> None:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    new_key = f"{prefix}{separator}{key}" if prefix else key
                    flatten(value, new_key)
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    new_key = f"{prefix}[{i}]"
                    flatten(item, new_key)
            else:
                result[prefix] = obj

        flatten(data)
        return {"data": result, "keys": len(result)}

    def _op_unflatten(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        if data is None:
            raise ValueError("Data is required for unflatten operation")
        result: dict[str, Any] = {}
        for key, value in data.items():
            self._set_path(result, key, value)
        return {"data": result}

    def _op_stringify(self, params: dict[str, Any]) -> dict[str, Any]:
        data = params.get("data")
        indent = params.get("indent", 2)
        if data is None:
            raise ValueError("Data is required for stringify operation")
        result = json.dumps(data, indent=indent, ensure_ascii=False, default=str)
        return {"json": result, "length": len(result)}

    def _op_extract(self, params: dict[str, Any]) -> dict[str, Any]:
        text = params.get("text", "")
        if not text:
            raise ValueError("Text is required for extract operation")
        extracted: list[dict[str, Any]] = []
        for block in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text):
            try:
                data = json.loads(block.strip())
                extracted.append(
                    {"data": data, "source": "code_block", "type": type(data).__name__}
                )
            except json.JSONDecodeError:
                pass
        for pattern in [r"\{[^{}]*\}", r"\[[^\[\]]*\]"]:
            for match in re.finditer(pattern, text):
                try:
                    data = json.loads(match.group())
                    extracted.append(
                        {"data": data, "source": "inline", "type": type(data).__name__}
                    )
                except json.JSONDecodeError:
                    pass
        return {"extracted": extracted, "count": len(extracted)}


_APPLY_TRANSFORMS: dict[str, Any] = {
    "identity": lambda v: v,
    "to_int": lambda v: int(v) if isinstance(v, bool) is False else v,
    "to_float": lambda v: float(v),
    "to_string": lambda v: str(v),
    "to_lower": lambda v: v.lower() if isinstance(v, str) else v,
    "to_upper": lambda v: v.upper() if isinstance(v, str) else v,
}


def _type_mismatch(v1: Any, v2: Any) -> bool:
    """功能：判断两个值类型是否「显著不同」（int/float 视为同族，bool 除外）。"""
    if isinstance(v1, bool) != isinstance(v2, bool):
        return True
    if isinstance(v1, type(v2)) or isinstance(v2, type(v1)):
        return False
    if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
        return False
    return True
