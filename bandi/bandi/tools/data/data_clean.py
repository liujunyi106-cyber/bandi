# 功能：data_clean——去重/缺失值填补/行列删缺/空白修剪/类型规范清洗工具
from __future__ import annotations

from typing import Any

import pandas as pd

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.data._data_io import as_dataframe, fail, ok, records_out

_OP_TYPES = [
    "remove_duplicates",
    "fill_missing",
    "drop_missing",
    "trim_whitespace",
    "normalize_types",
]
_FILL_STRATEGIES = ["value", "mean", "median", "mode", "ffill", "bfill"]
_NORMALIZE_TYPES = ["int", "float", "str", "bool", "datetime"]


class DataCleanTool(BaseTool):
    name = "data_clean"
    description = (
        "Clean and preprocess tabular data by removing duplicates, "
        "handling missing values, trimming whitespace, and normalizing data types."
    )
    category = ToolCategory.DATA
    feature_group = "data"
    capability_tags: list[str] = []
    risk_level = "low"
    is_concurrency_safe = True
    is_read_only = False
    is_idempotent = True
    aliases = ("clean", "preprocess", "data_preprocess")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {
                "type": "array",
                "description": "Input rows as a list of objects.",
                "items": {"type": "object"},
            },
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": _OP_TYPES},
                        "columns": {"type": "array", "items": {"type": "string"}},
                        "keep": {
                            "type": "string",
                            "enum": ["first", "last", "none"],
                            "default": "first",
                        },
                        "fill_value": {},
                        "fill_strategy": {
                            "type": "string",
                            "enum": _FILL_STRATEGIES,
                            "default": "value",
                        },
                        "axis": {"type": "string", "enum": ["rows", "columns"], "default": "rows"},
                        "how": {"type": "string", "enum": ["any", "all"], "default": "any"},
                        "thresh": {"type": "integer"},
                        "type_map": {
                            "type": "object",
                            "additionalProperties": {"type": "string", "enum": _NORMALIZE_TYPES},
                        },
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["data", "operations"],
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
        operations = params.get("operations") or []
        rows_before = len(df)
        columns_before = [str(c) for c in df.columns]
        applied: list[dict[str, Any]] = []

        for op in operations:
            result = self._apply(df, op)
            df = result[0]
            applied.append(result[1])

        output = records_out(df)
        return ok(
            output,
            rows_before=rows_before,
            rows_after=len(df),
            columns_before=columns_before,
            columns_after=[str(c) for c in df.columns],
            operations_applied=applied,
        )

    def _apply(self, df: pd.DataFrame, op: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any]]:
        op_type = op["type"]
        columns = op.get("columns") or []
        target_cols = columns if columns else [c for c in df.columns]
        if op_type == "remove_duplicates":
            return self._op_remove_duplicates(df, op)
        if op_type == "fill_missing":
            return self._op_fill_missing(df, op, target_cols)
        if op_type == "drop_missing":
            return self._op_drop_missing(df, op, columns)
        if op_type == "trim_whitespace":
            return self._op_trim(df, target_cols)
        if op_type == "normalize_types":
            return self._op_normalize(df, op)
        raise ValueError(f"Unknown operation: {op_type}")

    @staticmethod
    def _op_remove_duplicates(
        df: pd.DataFrame, op: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        rows_pre = len(df)
        subset = op.get("columns") or None
        keep = op.get("keep", "first")
        df = df.drop_duplicates(subset=subset, keep=False if keep == "none" else keep)
        removed = rows_pre - len(df)
        return df, {
            "type": "remove_duplicates",
            "columns": subset or "all",
            "duplicates_removed": removed,
        }

    def _op_fill_missing(
        self, df: pd.DataFrame, op: dict[str, Any], target_cols: list[str]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        strategy = op.get("fill_strategy", "value")
        fill_value = op.get("fill_value")
        filled_count = 0
        for col in target_cols:
            if col not in df.columns:
                continue
            missing_before = int(df[col].isna().sum())
            if missing_before == 0:
                continue
            if strategy == "value":
                df[col] = df[col].fillna(fill_value)
            elif strategy == "mean":
                df[col] = df[col].fillna(pd.to_numeric(df[col], errors="coerce").mean())
            elif strategy == "median":
                df[col] = df[col].fillna(pd.to_numeric(df[col], errors="coerce").median())
            elif strategy == "mode":
                mode_val = df[col].mode()
                if len(mode_val) > 0:
                    df[col] = df[col].fillna(mode_val.iloc[0])
            elif strategy == "ffill":
                df[col] = df[col].ffill()
            elif strategy == "bfill":
                df[col] = df[col].bfill()
            filled_count += missing_before - int(df[col].isna().sum())
        return df, {
            "type": "fill_missing",
            "strategy": strategy,
            "columns": target_cols,
            "values_filled": filled_count,
        }

    def _op_drop_missing(
        self, df: pd.DataFrame, op: dict[str, Any], columns: list[str]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        rows_pre = len(df)
        cols_pre = len(df.columns)
        axis = 0 if op.get("axis", "rows") == "rows" else 1
        how = op.get("how", "any")
        thresh = op.get("thresh")
        subset = columns or None
        if axis == 0:
            df = df.dropna(axis=0, how=how, thresh=thresh, subset=subset)
            return df, {"type": "drop_missing", "axis": "rows", "rows_dropped": rows_pre - len(df)}
        df = df.dropna(axis=1, how=how, thresh=thresh)
        return df, {
            "type": "drop_missing",
            "axis": "columns",
            "columns_dropped": cols_pre - len(df.columns),
        }

    @staticmethod
    def _op_trim(df: pd.DataFrame, target_cols: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
        trimmed: list[str] = []
        for col in target_cols:
            if col not in df.columns:
                continue
            df[col] = df[col].map(lambda x: x.strip() if isinstance(x, str) else x)
            trimmed.append(col)
        return df, {"type": "trim_whitespace", "columns_trimmed": trimmed}

    def _op_normalize(
        self, df: pd.DataFrame, op: dict[str, Any]
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        type_map = op.get("type_map") or {}
        converted: dict[str, str] = {}
        for col, dtype in type_map.items():
            if col not in df.columns:
                continue
            if dtype == "int":
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
            elif dtype == "float":
                df[col] = pd.to_numeric(df[col], errors="coerce")
            elif dtype == "str":
                df[col] = df[col].astype(str).replace("nan", None)
            elif dtype == "bool":
                df[col] = df[col].astype(bool)
            elif dtype == "datetime":
                df[col] = pd.to_datetime(df[col], errors="coerce")
            converted[col] = dtype
        return df, {"type": "normalize_types", "conversions": converted}
