# 功能：data_merge——内/外/左/右等值连接 + 行/列拼接工具
from __future__ import annotations

from typing import Any

import pandas as pd

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.data._data_io import as_dataframe, fail, ok, records_out

_JOIN_TYPES = ["inner", "outer", "left", "right", "cross"]
_VALIDATE_TYPES = ["one_to_one", "one_to_many", "many_to_one", "many_to_many"]


class DataMergeTool(BaseTool):
    name = "data_merge"
    description = (
        "Merge and join multiple datasets with support for inner, outer, left, "
        "and right joins, as well as concatenation operations."
    )
    category = ToolCategory.DATA
    feature_group = "data"
    capability_tags: list[str] = []
    risk_level = "low"
    is_concurrency_safe = True
    is_read_only = True
    is_idempotent = True
    aliases = ("merge", "join", "concat", "data_join")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "left_data": {
                "type": "array",
                "description": "Left dataset as list of records.",
                "items": {"type": "object"},
            },
            "right_data": {
                "type": "array",
                "description": "Right dataset as list of records.",
                "items": {"type": "object"},
            },
            "datasets": {
                "type": "array",
                "description": "Multiple datasets for concatenation (alternative to left/right).",
                "items": {"type": "array", "items": {"type": "object"}},
            },
            "operation": {
                "type": "string",
                "enum": ["merge", "concat"],
                "default": "merge",
            },
            "how": {"type": "string", "enum": _JOIN_TYPES, "default": "inner"},
            "on": {"type": "array", "items": {"type": "string"}},
            "left_on": {"type": "array", "items": {"type": "string"}},
            "right_on": {"type": "array", "items": {"type": "string"}},
            "suffixes": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["_left", "_right"],
                "minItems": 2,
                "maxItems": 2,
            },
            "indicator": {"type": "boolean", "default": False},
            "validate": {"type": "string", "enum": _VALIDATE_TYPES},
            "concat_axis": {"type": "string", "enum": ["rows", "columns"], "default": "rows"},
            "ignore_index": {"type": "boolean", "default": True},
        },
        "required": [],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            if params.get("operation", "merge") == "concat":
                return self._execute_concat(params)
            return self._execute_merge(params)
        except (ValueError, TypeError, KeyError) as e:
            return fail(str(e))

    def _execute_merge(self, params: dict[str, Any]) -> dict[str, Any]:
        left_df = as_dataframe(params.get("left_data"))
        right_df = as_dataframe(params.get("right_data"))
        if left_df.empty or right_df.empty:
            raise ValueError("Both left_data and right_data are required for merge operations")

        how = params.get("how", "inner")
        on = params.get("on")
        left_on = params.get("left_on")
        right_on = params.get("right_on")
        suffixes = tuple(params.get("suffixes", ["_left", "_right"]))
        indicator = params.get("indicator", False)

        for col in on or []:
            if col not in left_df.columns or col not in right_df.columns:
                raise ValueError(f"Column '{col}' not found in both datasets")
        for col in left_on or []:
            if col not in left_df.columns:
                raise ValueError(f"Column '{col}' not found in left dataset")
        for col in right_on or []:
            if col not in right_df.columns:
                raise ValueError(f"Column '{col}' not found in right dataset")

        kwargs: dict[str, Any] = {"how": how, "suffixes": suffixes, "indicator": indicator}
        if on:
            kwargs["on"] = on
        elif left_on and right_on:
            kwargs["left_on"] = left_on
            kwargs["right_on"] = right_on
        elif how != "cross":
            common = sorted(set(left_df.columns) & set(right_df.columns))
            if not common:
                raise ValueError(
                    "No common columns found. Specify 'on', 'left_on'/'right_on', or use 'cross' join."
                )
            kwargs["on"] = common
        if params.get("validate"):
            kwargs["validate"] = params["validate"]

        result_df = pd.merge(left_df, right_df, **kwargs)

        merge_stats: dict[str, Any] = {
            "left_rows": len(left_df),
            "right_rows": len(right_df),
            "result_rows": len(result_df),
            "join_type": how,
        }
        if indicator:
            counts = result_df["_merge"].value_counts().to_dict()
            merge_stats["merge_indicator"] = {
                "left_only": counts.get("left_only", 0),
                "right_only": counts.get("right_only", 0),
                "both": counts.get("both", 0),
            }

        output = records_out(result_df)
        return ok(
            output,
            columns=[str(c) for c in result_df.columns],
            merge_stats=merge_stats,
        )

    def _execute_concat(self, params: dict[str, Any]) -> dict[str, Any]:
        datasets = params.get("datasets")
        if not datasets:
            datasets = [params.get("left_data"), params.get("right_data")]
        real = [d for d in datasets if d]
        if len(real) < 2:
            raise ValueError("At least 2 datasets are required for concatenation")

        dfs = [as_dataframe(d) for d in real]
        axis = 0 if params.get("concat_axis", "rows") == "rows" else 1
        ignore_index = params.get("ignore_index", True)
        total_rows_before = sum(len(d) for d in dfs)

        if axis == 1 and ignore_index:
            result_df = pd.concat(dfs, axis=1, ignore_index=True)
        else:
            result_df = pd.concat(dfs, axis=axis, ignore_index=ignore_index)

        output = records_out(result_df)
        return ok(
            output,
            columns=[str(c) for c in result_df.columns],
            concat_stats={
                "datasets_count": len(dfs),
                "total_rows_before": total_rows_before,
                "result_rows": len(output),
                "axis": "rows" if axis == 0 else "columns",
            },
        )
