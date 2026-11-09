# 功能：data_aggregate——groupby/pivot/describe/value_counts/rolling 聚合工具
from __future__ import annotations

from typing import Any

import pandas as pd

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.data._data_io import as_dataframe, fail, ok, records_out

_AGG_FUNCS = [
    "sum",
    "mean",
    "avg",
    "count",
    "min",
    "max",
    "std",
    "var",
    "median",
    "first",
    "last",
    "nunique",
]
_PIVOT_FUNCS = ["sum", "mean", "count", "min", "max", "first", "last"]
_ROLLING_FUNCS = ["sum", "mean", "min", "max", "std", "var"]


class DataAggregateTool(BaseTool):
    name = "data_aggregate"
    description = (
        "Aggregate and summarize tabular data with group by operations, "
        "aggregation functions (sum, avg, count, min, max), and pivot tables."
    )
    category = ToolCategory.DATA
    feature_group = "data"
    capability_tags: list[str] = []
    risk_level = "low"
    is_concurrency_safe = True
    is_read_only = True
    is_idempotent = True
    aliases = ("aggregate", "groupby", "summarize")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {
                "type": "array",
                "description": "Input rows as a list of objects.",
                "items": {"type": "object"},
            },
            "operation": {
                "type": "string",
                "enum": ["groupby", "pivot", "describe", "value_counts", "rolling"],
                "default": "groupby",
            },
            "group_by": {"type": "array", "items": {"type": "string"}},
            "aggregations": {
                "type": "object",
                "additionalProperties": {
                    "oneOf": [
                        {"type": "string", "enum": _AGG_FUNCS},
                        {"type": "array", "items": {"type": "string", "enum": _AGG_FUNCS}},
                    ]
                },
            },
            "pivot_index": {"type": "array", "items": {"type": "string"}},
            "pivot_columns": {"type": "array", "items": {"type": "string"}},
            "pivot_values": {"type": "array", "items": {"type": "string"}},
            "pivot_aggfunc": {"type": "string", "enum": _PIVOT_FUNCS, "default": "mean"},
            "fill_value": {},
            "column": {"type": "string", "description": "For value_counts: column to count."},
            "normalize": {"type": "boolean", "default": False},
            "top_n": {"type": "integer"},
            "window_size": {"type": "integer"},
            "window_column": {"type": "string"},
            "window_func": {"type": "string", "enum": _ROLLING_FUNCS, "default": "mean"},
            "sort_by": {"type": "string"},
            "sort_ascending": {"type": "boolean", "default": True},
        },
        "required": ["data"],
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
        operation = params.get("operation", "groupby")
        input_rows = len(df)

        if not input_rows:
            return ok([], operation=operation, input_rows=0, output_rows=0, columns=[])

        if operation == "groupby":
            result_df = self._groupby(df, params)
        elif operation == "pivot":
            result_df = self._pivot(df, params)
        elif operation == "describe":
            result_df = self._describe(df)
        elif operation == "value_counts":
            result_df = self._value_counts(df, params)
        elif operation == "rolling":
            result_df = self._rolling(df, params)
        else:
            raise ValueError(f"Unknown operation: {operation}")

        sort_by = params.get("sort_by")
        if sort_by and sort_by in result_df.columns:
            result_df = result_df.sort_values(
                by=sort_by, ascending=params.get("sort_ascending", True)
            )

        columns = [str(c) for c in result_df.columns]
        output = records_out(result_df)
        return ok(
            output,
            operation=operation,
            input_rows=input_rows,
            output_rows=len(output),
            columns=columns,
        )

    def _groupby(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        group_by = params.get("group_by") or []
        if not group_by:
            raise ValueError("groupby operation requires 'group_by' columns")
        missing = set(group_by) - set(df.columns)
        if missing:
            raise ValueError(f"Group by columns not found: {sorted(missing)}")

        aggregations = params.get("aggregations") or {}
        agg_map: dict[str, Any] = {}
        for col, funcs in aggregations.items():
            if col not in df.columns:
                continue
            func_list = [funcs] if isinstance(funcs, str) else list(funcs)
            agg_map[col] = ["mean" if f == "avg" else f for f in func_list]

        if not agg_map:
            numeric_cols = df.select_dtypes(include=["number"]).columns
            agg_cols = [c for c in numeric_cols if c not in group_by]
            agg_map = {col: ["sum", "mean", "count"] for col in agg_cols[:5]}
        if not agg_map:
            agg_map = {group_by[0]: ["count"]}

        grouped = df.groupby(group_by, as_index=False).agg(agg_map)
        if isinstance(grouped.columns, pd.MultiIndex):
            grouped.columns = [f"{c0}_{c1}" if c1 else str(c0) for c0, c1 in grouped.columns]
        return grouped

    def _pivot(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        pivot_index = params.get("pivot_index") or []
        pivot_columns = params.get("pivot_columns") or []
        pivot_values = params.get("pivot_values") or []
        if not pivot_index or not pivot_columns:
            raise ValueError("pivot operation requires 'pivot_index' and 'pivot_columns'")
        for col in pivot_index:
            if col not in df.columns:
                raise ValueError(f"Pivot index column not found: {col}")
        for col in pivot_columns:
            if col not in df.columns:
                raise ValueError(f"Pivot column not found: {col}")
        aggfunc = (
            "mean" if params.get("pivot_aggfunc") == "avg" else params.get("pivot_aggfunc", "mean")
        )

        kwargs: dict[str, Any] = {
            "index": pivot_index[0] if len(pivot_index) == 1 else pivot_index,
            "columns": pivot_columns[0] if len(pivot_columns) == 1 else pivot_columns,
            "aggfunc": aggfunc,
        }
        if pivot_values:
            for col in pivot_values:
                if col not in df.columns:
                    raise ValueError(f"Pivot value column not found: {col}")
            kwargs["values"] = pivot_values[0] if len(pivot_values) == 1 else pivot_values
        if params.get("fill_value") is not None:
            kwargs["fill_value"] = params["fill_value"]

        pivot_df = pd.pivot_table(df, **kwargs).reset_index()
        if isinstance(pivot_df.columns, pd.MultiIndex):
            pivot_df.columns = [
                "_".join(str(c) for c in col).strip("_") for col in pivot_df.columns
            ]
        return pivot_df

    @staticmethod
    def _describe(df: pd.DataFrame) -> pd.DataFrame:
        desc = df.describe(include="all").T.reset_index()
        return desc.rename(columns={"index": "column"})

    def _value_counts(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        column = params.get("column")
        if not column:
            raise ValueError("value_counts operation requires 'column'")
        if column not in df.columns:
            raise ValueError(f"Column '{column}' not found")
        counts = df[column].value_counts(normalize=params.get("normalize", False))
        top_n = params.get("top_n")
        if top_n:
            counts = counts.head(int(top_n))
        result = counts.reset_index()
        result.columns = [column, "proportion" if params.get("normalize") else "count"]
        return result

    def _rolling(self, df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
        window_size = params.get("window_size")
        window_column = params.get("window_column")
        window_func = params.get("window_func", "mean")
        if not window_size or not window_column:
            raise ValueError("rolling operation requires 'window_size' and 'window_column'")
        if window_column not in df.columns:
            raise ValueError(f"Column '{window_column}' not found")
        result = df.copy()
        col_name = f"{window_column}_rolling_{window_func}_{window_size}"
        rolling = df[window_column].rolling(window=int(window_size), min_periods=1)
        result[col_name] = getattr(rolling, window_func)()
        return result
