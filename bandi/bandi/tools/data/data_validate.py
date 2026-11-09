# 功能：data_validate——列存在/必填/非空/类型/唯一性/行数/schema 校验工具
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import pandas as pd

from bandi.tools.base import BaseTool, ToolCategory, ToolPermissionContext
from bandi.tools.data._data_io import as_dataframe, fail, ok, records_out

_VALIDATORS = [
    "required",
    "not_null",
    "type",
    "unique",
    "range",
    "regex",
    "allowed_values",
    "date_format",
    "min_length",
    "max_length",
    "not_empty",
]


class DataValidateTool(BaseTool):
    name = "data_validate"
    description = (
        "Validate tabular data against schema constraints including required columns, "
        "null checks, type checks, unique constraints, value ranges, and regex patterns."
    )
    category = ToolCategory.DATA
    feature_group = "data"
    capability_tags: list[str] = []
    risk_level = "low"
    is_concurrency_safe = True
    is_read_only = True
    is_idempotent = True
    aliases = ("validate", "data_check", "check_data")
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "data": {
                "type": "array",
                "description": "Input rows as a list of objects.",
                "items": {"type": "object"},
            },
            "schema": {"type": "object"},
            "column_rules": {
                "type": "object",
                "description": "Mapping column -> list of validator configs.",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            "row_count": {
                "type": "object",
                "description": "Row count constraint, e.g. {'gte': 1, 'lte': 100} or {'between': [1, 100]}.",
            },
            "unique_together": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "fail_fast": {"type": "boolean", "default": False},
            "return_valid_rows": {"type": "boolean", "default": True},
            "max_errors": {"type": "integer", "default": 100},
        },
        "required": ["data"],
        "additionalProperties": False,
    }

    async def execute(
        self, params: dict[str, Any], context: ToolPermissionContext | None = None
    ) -> dict[str, Any]:
        try:
            return self._execute_sync(params)
        except (ValueError, TypeError, KeyError, IndexError) as e:
            return fail(str(e))

    def _execute_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        df = as_dataframe(params.get("data"))
        schema = params.get("schema") or {}
        column_rules = params.get("column_rules") or {}
        row_count_rule = params.get("row_count") or {}
        unique_together = params.get("unique_together") or []
        fail_fast = params.get("fail_fast", False)
        return_valid_rows = params.get("return_valid_rows", True)
        max_errors = int(params.get("max_errors", 100))

        errors_by_row: dict[int, list[dict[str, Any]]] = defaultdict(list)
        overall_errors: list[dict[str, Any]] = []
        validators_applied: list[str] = []

        if "columns" in schema:
            validators_applied.append("columns")
        if "required_columns" in schema:
            validators_applied.append("required_columns")

        missing_required = self._check_required_columns(df, schema)
        for col in missing_required:
            overall_errors.append(
                {
                    "row": None,
                    "column": col,
                    "error": "required_columns",
                    "message": f"Required column '{col}' is missing",
                }
            )

        combined_rules: dict[str, list[dict[str, Any]]] = dict(column_rules)
        schema_rules = schema.get("column_rules") or {}
        for col, rules in schema_rules.items():
            combined_rules.setdefault(col, []).extend(rules)

        for col, rules in combined_rules.items():
            for rule in rules:
                validators_applied.append(f"{col}.{rule.get('type')}")
                row_errors = self._validate_column(df, col, rule)
                for row_idx, message in row_errors:
                    errors_by_row[row_idx].append(
                        {
                            "row": row_idx,
                            "column": col,
                            "error": rule.get("type"),
                            "message": message,
                        }
                    )

        if fail_fast and (errors_by_row or overall_errors):
            return self._emit_result(
                df,
                errors_by_row,
                overall_errors,
                return_valid_rows,
                max_errors,
                validators_applied,
            )

        unique_sets = self._check_unique_together(df, unique_together)
        for cols in unique_sets:
            validators_applied.append(f"unique_together:{','.join(cols)}")
            for row_idx, dup_count in unique_sets[cols]:
                errors_by_row[row_idx].append(
                    {
                        "row": row_idx,
                        "column": ",".join(cols),
                        "error": "unique_together",
                        "message": f"Duplicate values in {cols}",
                    }
                )

        if row_count_rule:
            validators_applied.append(f"row_count:{sorted(row_count_rule.keys())}")
            rc_errors = self._check_row_count(len(df), row_count_rule)
            overall_errors.extend(rc_errors)

        return self._emit_result(
            df,
            errors_by_row,
            overall_errors,
            return_valid_rows,
            max_errors,
            validators_applied,
        )

    @staticmethod
    def _emit_result(
        df: pd.DataFrame,
        errors_by_row: dict[int, list[dict[str, Any]]],
        overall_errors: list[dict[str, Any]],
        return_valid_rows: bool,
        max_errors: int,
        validators_applied: list[str],
    ) -> dict[str, Any]:
        # 功能：聚合行级/整体错误并包装结果——invalid_rows 按全量行错误计算（截断仅限 errors 展示）
        all_row_errors = [e for idx in sorted(errors_by_row) for e in errors_by_row[idx]]
        flattened = (all_row_errors + overall_errors)[:max_errors]

        invalid_rows = {e["row"] for e in all_row_errors if isinstance(e.get("row"), int)}
        valid_rows = records_out(df.drop(index=sorted(invalid_rows))) if return_valid_rows else []
        total_errors = len(all_row_errors) + len(overall_errors)
        is_valid = total_errors == 0
        valid_row_count = len(df) - len(invalid_rows)

        return ok(
            valid_rows,
            is_valid=is_valid,
            total_errors=total_errors,
            total_rows=len(df),
            valid_rows_count=valid_row_count if return_valid_rows else None,
            invalid_rows_count=len(invalid_rows),
            errors=flattened,
            validators_applied=validators_applied,
        )

    @staticmethod
    def _check_required_columns(df: pd.DataFrame, schema: dict[str, Any]) -> list[str]:
        required: list[str] = schema.get("required_columns") or schema.get("columns") or []
        return [c for c in required if c not in df.columns]

    def _validate_column(
        self, df: pd.DataFrame, col: str, rule: dict[str, Any]
    ) -> list[tuple[int, str]]:
        rule_type = rule.get("type")
        if rule_type not in _VALIDATORS or col not in df.columns:
            return []
        errors: list[tuple[int, str]] = []
        if rule_type == "required":
            missing = df.index[df[col].isna()].tolist()
            errors.extend((int(i), f"Required column '{col}' has null at row {i}") for i in missing)
        elif rule_type == "not_null":
            missing = df.index[df[col].isna()].tolist()
            errors.extend(
                (int(i), f"Null value found in column '{col}' at row {i}") for i in missing
            )
        elif rule_type == "not_empty":
            empty_mask = df[col].fillna("").astype(str).str.strip().eq("")
            empty_idx = df.index[empty_mask].tolist()
            errors.extend((int(i), f"Empty value in column '{col}' at row {i}") for i in empty_idx)
        elif rule_type == "type":
            expected = rule.get("expect")
            bad = df.index[~self._type_ok(df[col], expected)].tolist()
            errors.extend((int(i), f"Column '{col}' at row {i} is not {expected}") for i in bad)
        elif rule_type == "unique":
            dup_mask = df[col].duplicated(keep=False)
            dup_idx = df.index[dup_mask].tolist()
            errors.extend(
                (int(i), f"Duplicate value in column '{col}' at row {i}") for i in dup_idx
            )
        elif rule_type == "range":
            lo = rule.get("min")
            hi = rule.get("max")
            numeric = pd.to_numeric(df[col], errors="coerce")
            if lo is not None:
                bad = df.index[numeric.lt(lo)].tolist()
                errors.extend(
                    (int(i), f"Value {df[col].iloc[i]} < {lo} in column '{col}' at row {i}")
                    for i in bad
                )
            if hi is not None:
                bad = df.index[numeric.gt(hi)].tolist()
                errors.extend(
                    (int(i), f"Value {df[col].iloc[i]} > {hi} in column '{col}' at row {i}")
                    for i in bad
                )
        elif rule_type == "regex":
            import re

            pattern = rule.get("pattern")
            try:
                regex = re.compile(str(pattern))
            except re.error:
                return [(int(i), f"Invalid regex pattern '{pattern}'") for i in df.index]
            for i in df.index:
                v = df[col].iloc[i]
                if v is None or not regex.search(str(v)):
                    errors.append(
                        (
                            int(i),
                            f"Value '{v}' does not match regex '{pattern}' in '{col}' at row {i}",
                        )
                    )
        elif rule_type == "allowed_values":
            allowed = set(rule.get("allowed", []))
            for i in df.index:
                v = df[col].iloc[i]
                if v not in allowed:
                    errors.append(
                        (int(i), f"Value '{v}' not in allowed set for '{col}' at row {i}")
                    )
        elif rule_type == "date_format":
            fmt = rule.get("format")
            parsed = pd.to_datetime(df[col], format=fmt, errors="coerce")
            bad = df.index[parsed.isna() & df[col].notna()].tolist()
            errors.extend(
                (int(i), f"Column '{col}' at row {i} does not match date format '{fmt}'")
                for i in bad
            )
        elif rule_type in ("min_length", "max_length"):
            length = int(rule.get("length") or rule.get("value") or 0)
            op = "lt" if rule_type == "min_length" else "gt"
            for i in df.index:
                v = df[col].iloc[i]
                vlen = len(str(v if v is not None else ""))
                if op == "lt" and v is not None and vlen < length:
                    errors.append((int(i), f"Value length {vlen} < {length} in '{col}' at row {i}"))
                elif op == "gt" and v is not None and vlen > length:
                    errors.append((int(i), f"Value length {vlen} > {length} in '{col}' at row {i}"))
        return errors

    @staticmethod
    def _type_ok(series: pd.Series, expected: str | None) -> pd.Series:
        # 功能：返回每行是否满足类型（integer/number/string/boolean/datetime/object）
        if expected == "integer":
            numeric = pd.to_numeric(series, errors="coerce")
            return numeric.notna() & numeric.mod(1).eq(0)
        if expected == "number":
            return pd.to_numeric(series, errors="coerce").notna()
        if expected == "string":
            return pd.Series(
                [v is not None and isinstance(v, str) for v in series], index=series.index
            )
        if expected == "boolean":
            return pd.Series(
                [v is not None and isinstance(v, bool) for v in series], index=series.index
            )
        if expected == "datetime":
            return pd.to_datetime(series, errors="coerce").notna()
        if expected == "object":
            return pd.Series([v is not None for v in series], index=series.index)
        return pd.Series([True] * len(series), index=series.index)

    @staticmethod
    def _check_unique_together(
        df: pd.DataFrame, unique_together: list[list[str]]
    ) -> dict[tuple[str, ...], list[tuple[int, int]]]:
        # 功能：组合唯一性检查——返回 {列组: [(重复行索引, 该值出现次数), ...]}
        result: dict[tuple[str, ...], list[tuple[int, int]]] = {}
        for cols in unique_together:
            key = tuple(cols)
            missing = set(cols) - set(df.columns)
            if missing:
                continue
            counts = Counter(map(tuple, df[list(cols)].itertuples(index=False, name=None)))
            flagged = {
                idx: count
                for idx, row in enumerate(df[list(cols)].itertuples(index=False, name=None))
                for (val, count) in [(row, counts[row])]
                if count > 1
            }
            if flagged:
                result[key] = [(int(idx), count) for idx, count in flagged.items()]
        return result

    @staticmethod
    def _check_row_count(total: int, rule: dict[str, Any]) -> list[dict[str, Any]]:
        # 功能：整体行数约束 eq/gte/lte/gt/lt/between
        errors: list[dict[str, Any]] = []
        for op, value in rule.items():
            if op == "eq" and total != int(value):
                errors.append(
                    {
                        "row": None,
                        "column": None,
                        "error": "row_count",
                        "message": f"Row count {total} != {value}",
                    }
                )
            elif op == "gte" and total < int(value):
                errors.append(
                    {
                        "row": None,
                        "column": None,
                        "error": "row_count",
                        "message": f"Row count {total} < {value}",
                    }
                )
            elif op == "lte" and total > int(value):
                errors.append(
                    {
                        "row": None,
                        "column": None,
                        "error": "row_count",
                        "message": f"Row count {total} > {value}",
                    }
                )
            elif op == "gt" and total <= int(value):
                errors.append(
                    {
                        "row": None,
                        "column": None,
                        "error": "row_count",
                        "message": f"Row count {total} <= {value}",
                    }
                )
            elif op == "lt" and total >= int(value):
                errors.append(
                    {
                        "row": None,
                        "column": None,
                        "error": "row_count",
                        "message": f"Row count {total} >= {value}",
                    }
                )
            elif op == "between":
                lo, hi = value[0], value[1]
                if total < int(lo) or total > int(hi):
                    errors.append(
                        {
                            "row": None,
                            "column": None,
                            "error": "row_count",
                            "message": f"Row count {total} not in [{lo}, {hi}]",
                        }
                    )
        return errors
