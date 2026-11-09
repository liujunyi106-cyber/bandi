# 功能：data_transform——rename/cast/map/derive/select/drop/reorder/string/split/combine/extract 变换工具
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.data._data_io import as_dataframe, fail, ok, records_out

_CAST_TYPES = ["int", "float", "str", "bool", "datetime", "date"]
_STRING_TRANSFORMS = ["upper", "lower", "title", "strip", "capitalize"]
_SAFE_FUNCS: dict[str, Any] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
}


class DataTransformTool(BaseTool):
    name = "data_transform"
    description = (
        "Transform tabular data with operations including column renaming, "
        "type casting, value mapping, derived columns, and string transformations."
    )
    category = ToolCategory.DATA
    feature_group = "data"
    capability_tags: list[str] = []
    risk_level = "low"
    is_concurrency_safe = True
    is_read_only = False
    is_idempotent = True
    aliases = ("transform", "reshape", "data_reshape")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {
                "type": "array",
                "description": "Input rows as a list of objects.",
                "items": {"type": "object"},
            },
            "transformations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "rename",
                                "cast",
                                "map_values",
                                "derive",
                                "select",
                                "drop",
                                "reorder",
                                "string_transform",
                                "split",
                                "combine",
                                "extract",
                            ],
                        },
                        "columns": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "column": {"type": "string"},
                        "target_type": {"type": "string", "enum": _CAST_TYPES},
                        "datetime_format": {"type": "string"},
                        "mapping": {"type": "object"},
                        "default": {},
                        "expression": {"type": "string"},
                        "new_column": {"type": "string"},
                        "select_columns": {"type": "array", "items": {"type": "string"}},
                        "drop_columns": {"type": "array", "items": {"type": "string"}},
                        "transform_type": {"type": "string", "enum": _STRING_TRANSFORMS},
                        "delimiter": {"type": "string"},
                        "split_index": {"type": "integer"},
                        "combine_columns": {"type": "array", "items": {"type": "string"}},
                        "combine_separator": {"type": "string", "default": " "},
                        "pattern": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["data", "transformations"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            return self._execute_sync(params)
        except (ValueError, TypeError, KeyError) as e:
            return fail(str(e))

    def _execute_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        df = as_dataframe(params.get("data"))
        transformations = params.get("transformations") or []
        columns_before = [str(c) for c in df.columns]
        applied: list[dict[str, Any]] = []

        for transform in transformations:
            t_type = transform["type"]
            result = self._apply(df, transform)
            df = result[0]
            applied.append({"type": t_type, **result[1]})

        output = records_out(df)
        return ok(
            output,
            columns_before=columns_before,
            columns_after=[str(c) for c in df.columns],
            transformations_applied=applied,
        )

    def _apply(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        t_type = transform["type"]
        if t_type == "rename":
            return self._t_rename(df, transform)
        if t_type == "cast":
            return self._t_cast(df, transform)
        if t_type == "map_values":
            return self._t_map(df, transform)
        if t_type == "derive":
            return self._t_derive(df, transform)
        if t_type == "select":
            return self._t_select(df, transform)
        if t_type == "drop":
            return self._t_drop(df, transform)
        if t_type == "reorder":
            return self._t_reorder(df, transform)
        if t_type == "string_transform":
            return self._t_string(df, transform)
        if t_type == "split":
            return self._t_split(df, transform)
        if t_type == "combine":
            return self._t_combine(df, transform)
        if t_type == "extract":
            return self._t_extract(df, transform)
        raise ValueError(f"Unknown transformation: {t_type}")

    @staticmethod
    def _t_rename(
        df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        mapping = transform.get("columns") or {}
        missing = sorted(set(mapping) - set(df.columns))
        if missing:
            raise ValueError(f"Columns not found: {missing}")
        renamed = {k: v for k, v in mapping.items() if k in df.columns}
        return df.rename(columns=renamed), {"renamed": renamed}

    def _t_cast(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        column = transform.get("column")
        target_type = transform.get("target_type")
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found")
        if target_type == "int":
            df[column] = pd.to_numeric(df[column], errors="coerce").astype("Int64")
        elif target_type == "float":
            df[column] = pd.to_numeric(df[column], errors="coerce")
        elif target_type == "str":
            df[column] = df[column].astype(str).replace("nan", None)
        elif target_type == "bool":
            df[column] = df[column].astype(bool)
        elif target_type == "datetime":
            fmt = transform.get("datetime_format")
            df[column] = pd.to_datetime(df[column], format=fmt, errors="coerce")
        elif target_type == "date":
            df[column] = pd.to_datetime(df[column], errors="coerce").dt.date
        else:
            raise ValueError(f"Unsupported target_type: {target_type}")
        return df, {"column": column, "to_type": target_type}

    def _t_map(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        column = transform.get("column")
        mapping = transform.get("mapping") or {}
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found")
        default = transform.get("default")
        if default is not None:
            df[column] = df[column].map(mapping).fillna(default)
        else:
            df[column] = df[column].replace(mapping)
        return df, {"column": column, "mappings_count": len(mapping)}

    def _t_derive(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        expression = transform.get("expression")
        new_column = transform.get("new_column")
        if not expression or not new_column:
            raise ValueError("derive requires 'expression' and 'new_column'")
        env: dict[str, Any] = dict(_SAFE_FUNCS)
        for col in df.columns:
            env[col] = df[col]
        try:
            # → 改为 ast 白名单求值器（列引用/字面量/算术/比较/逻辑/白名单函数，禁一切逃逸构造）
            from bandi.tools.data._expr import evaluate_expression

            result = evaluate_expression(expression, env, _SAFE_FUNCS)
        except ValueError as exc:
            raise ValueError(f"Invalid expression '{expression}': {exc}") from exc
        df[new_column] = result
        return df, {"new_column": new_column, "expression": expression}

    def _t_select(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        select_columns = transform.get("select_columns") or []
        missing = sorted(set(select_columns) - set(df.columns))
        if missing:
            raise ValueError(f"Columns not found: {missing}")
        return df[select_columns], {"columns": select_columns}

    def _t_drop(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        drop_columns = transform.get("drop_columns") or []
        existing = [c for c in drop_columns if c in df.columns]
        return df.drop(columns=existing), {"dropped": existing}

    def _t_reorder(
        self, df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        select_columns = transform.get("select_columns") or []
        new_order = [c for c in select_columns if c in df.columns]
        remaining = [c for c in df.columns if c not in new_order]
        return df[new_order + remaining], {"new_order": new_order + remaining}

    @staticmethod
    def _t_string(
        df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        column = transform.get("column")
        transform_type = transform.get("transform_type")
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found")
        series = df[column].astype(str)
        if transform_type == "upper":
            df[column] = series.str.upper()
        elif transform_type == "lower":
            df[column] = series.str.lower()
        elif transform_type == "title":
            df[column] = series.str.title()
        elif transform_type == "strip":
            df[column] = series.str.strip()
        elif transform_type == "capitalize":
            df[column] = series.str.capitalize()
        else:
            raise ValueError(f"Unsupported transform_type: {transform_type}")
        return df, {"column": column, "transform_type": transform_type}

    @staticmethod
    def _t_split(
        df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        column = transform.get("column")
        delimiter = transform.get("delimiter", " ")
        split_index = int(transform.get("split_index", 0))
        new_column = transform.get("new_column") or column
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found")

        def _part(value: Any) -> Any:
            parts = str(value).split(delimiter)
            return parts[split_index] if len(parts) > split_index else None

        df[new_column] = df[column].map(_part)
        return df, {
            "source_column": column,
            "target_column": new_column,
            "split_index": split_index,
        }

    @staticmethod
    def _t_combine(
        df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        combine_columns = transform.get("combine_columns") or []
        new_column = transform.get("new_column")
        separator = transform.get("combine_separator", " ")
        if not new_column:
            raise ValueError("combine requires 'new_column'")
        missing = sorted(set(combine_columns) - set(df.columns))
        if missing:
            raise ValueError(f"Columns not found: {missing}")

        def _join(row: Any) -> str:
            return separator.join(str(row[c]) for c in combine_columns)

        df[new_column] = df.apply(_join, axis=1)
        return df, {"source_columns": combine_columns, "new_column": new_column}

    @staticmethod
    def _t_extract(
        df: pd.DataFrame, transform: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        column = transform.get("column")
        pattern = transform.get("pattern")
        new_column = transform.get("new_column")
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found")
        if not pattern:
            raise ValueError("extract requires 'pattern'")
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e
        target_col = new_column or column

        def _match(value: Any) -> Any:
            m = regex.search(str(value))
            return m.group(1) if m and m.groups() else (m.group(0) if m else None)

        df[target_col] = df[column].map(_match)
        return df, {"source_column": column, "target_column": target_col, "pattern": pattern}
