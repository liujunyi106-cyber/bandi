# 功能：data 工具族契约族——records 内联 I/O/中文/空数据/多列组合/门控/装配
# 行为族级覆盖（DoD-10）：aggregate 四操作 + merge 三种 join + transform 多变换 + clean 三操作 + validate 多规则
from __future__ import annotations

from typing import Any

import pytest

from bandi.tools.base import ToolPermissionContext

_ROWS = [
    {"dept": "销售", "name": "张伟", "amount": 100, "date": "2026-01-05"},
    {"dept": "销售", "name": "李娜", "amount": None, "date": "2026-01-06"},
    {"dept": "工程", "name": "王强", "amount": 300, "date": "2026-01-07"},
    {"dept": "工程", "name": "赵敏", "amount": 200, "date": "2026-01-08"},
]


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(user_id="unit-test", role="tester", mode="execute")


def _tools() -> Any:
    from bandi.tools.data import (
        DataAggregateTool,
        DataCleanTool,
        DataMergeTool,
        DataTransformTool,
        DataValidateTool,
    )

    return (
        DataAggregateTool(),
        DataMergeTool(),
        DataTransformTool(),
        DataCleanTool(),
        DataValidateTool(),
    )


# ---------- aggregate 族 ----------


@pytest.mark.asyncio
async def test_aggregate_groupby_numeric_stats() -> None:
    agg, *_ = _tools()
    result = await agg.execute(
        {"data": _ROWS, "operation": "groupby", "group_by": ["dept"]}, _ctx()
    )
    assert result["success"] is True
    by_dept = {r["dept"]: r for r in result["data"]}
    assert by_dept["销售"]["amount_sum"] == 100.0
    assert by_dept["工程"]["amount_sum"] == 500.0
    assert result["input_rows"] == 4
    assert result["output_rows"] == 2


@pytest.mark.asyncio
async def test_aggregate_groupby_missing_col_is_failure() -> None:
    agg, *_ = _tools()
    result = await agg.execute(
        {"data": _ROWS, "operation": "groupby", "group_by": ["不存在"]}, _ctx()
    )
    assert result["success"] is False
    assert "不存在" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_aggregate_describe_gives_stats() -> None:
    agg, *_ = _tools()
    result = await agg.execute({"data": _ROWS, "operation": "describe"}, _ctx())
    assert result["success"] is True
    cols = {r["column"] for r in result["data"]}
    assert "amount" in cols
    assert "dept" in cols


@pytest.mark.asyncio
async def test_aggregate_value_counts() -> None:
    agg, *_ = _tools()
    result = await agg.execute(
        {"data": _ROWS, "operation": "value_counts", "column": "dept"}, _ctx()
    )
    assert result["success"] is True
    counts = {r["dept"]: r["count"] for r in result["data"]}
    assert counts == {"销售": 2, "工程": 2}


@pytest.mark.asyncio
async def test_aggregate_pivot() -> None:
    agg, *_ = _tools()
    result = await agg.execute(
        {
            "data": _ROWS,
            "operation": "pivot",
            "pivot_index": ["dept"],
            "pivot_columns": ["name"],
            "pivot_values": ["amount"],
            "pivot_aggfunc": "sum",
        },
        _ctx(),
    )
    assert result["success"] is True
    row = result["data"][0]
    assert row.get("张伟") == 100.0 or row.get("王强") == 300.0


# ---------- merge 族 ----------


@pytest.mark.asyncio
async def test_merge_left_join_surfaces_all_left() -> None:
    _, merge, *_ = _tools()
    result = await merge.execute(
        {
            "left_data": [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}],
            "right_data": [{"id": 1, "x": "A"}, {"id": 3, "x": "C"}],
            "on": ["id"],
            "how": "left",
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["merge_stats"]["result_rows"] == 2
    assert result["merge_stats"]["left_rows"] == 2


@pytest.mark.asyncio
async def test_merge_concat_rows() -> None:
    _, merge, *_ = _tools()
    result = await merge.execute(
        {"left_data": _ROWS[:2], "right_data": _ROWS[2:], "operation": "concat"}, _ctx()
    )
    assert result["success"] is True
    assert result["concat_stats"]["result_rows"] == 4
    assert len(result["data"]) == 4


@pytest.mark.asyncio
async def test_merge_cross_join_without_key() -> None:
    _, merge, *_ = _tools()
    result = await merge.execute(
        {
            "left_data": [{"a": 1}, {"a": 2}],
            "right_data": [{"b": 10}],
            "operation": "merge",
            "how": "cross",
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["merge_stats"]["result_rows"] == 2


@pytest.mark.asyncio
async def test_merge_no_common_col_fails() -> None:
    _, merge, *_ = _tools()
    result = await merge.execute({"left_data": [{"a": 1}], "right_data": [{"b": 2}]}, _ctx())
    assert result["success"] is False
    assert (
        "c" in str(result.get("error", "")).lower()
        or "common" in str(result.get("error", "")).lower()
    )


# ---------- transform 族 ----------


@pytest.mark.asyncio
async def test_transform_rename_and_derive() -> None:
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [
                {"type": "rename", "columns": {"name": "fullname"}},
                {"type": "derive", "expression": "amount * 2", "new_column": "amount_dbl"},
            ],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert "fullname" in result["columns_after"]
    assert "name" not in result["columns_after"]
    assert result["data"][0]["amount_dbl"] == 200.0


@pytest.mark.asyncio
async def test_derive_boolop_vector_and_or() -> None:
    """复核修复：and/or 逻辑在列（Series）场景必须逐元素生效——
    修复前 _ALLOWED_BOOL_OPS 用 all()/any() 组合 Series 触发 pandas 真值歧义（假拒绝）。
    语义：每行结果 == 同表达式对行内标量求值的 Python 结果（实现无关断言）。"""
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [
                {
                    "type": "derive",
                    "expression": "amount >= 150 and amount <= 250",
                    "new_column": "mid",
                },
                {
                    "type": "derive",
                    "expression": "amount < 150 or amount > 250",
                    "new_column": "edge",
                },
            ],
        },
        _ctx(),
    )
    assert result["success"] is True, f"and/or 必须可用: {result.get('error')}"
    rows = result["data"]
    assert len(rows) >= 3, "需至少 3 行样本覆盖三态"
    for i, row in enumerate(rows):
        amt = row["amount"]
        if amt is None or amt != amt:  # None/NaN 行：不抛不崩即可（结果 False/NaN 均接受）
            assert row["mid"] in (False, None) or row["mid"] != row["mid"]
            continue
        assert row["mid"] == (150 <= amt <= 250), f"row{i} and 语义错: {amt} -> {row['mid']}"
        assert row["edge"] == (amt < 150 or amt > 250), f"row{i} or 语义错: {amt} -> {row['edge']}"


@pytest.mark.asyncio
async def test_derive_rejects_attribute_escape() -> None:
    """修复：derive 表达式拒绝属性访问/下标逃逸
    __class__.__mro__ 链逃逸 RCE）——fail-closed 报错，绝不执行。"""
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [
                {"type": "derive", "expression": "amount.__class__.__mro__", "new_column": "x"},
            ],
        },
        _ctx(),
    )
    assert result["success"] is False
    err = str(result.get("error") or "")
    assert "expression" in err or "不支持" in err, f"必须显式拒绝属性访问: {err}"

    # 经典 eval 沙箱逃逸链同样被拒（字面量构造属性链）
    result2 = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [
                {"type": "derive", "expression": "().__class__.__bases__[0]", "new_column": "x"},
            ],
        },
        _ctx(),
    )
    assert result2["success"] is False
    assert "data" not in result2, "逃逸表达式不得产出数据列"


@pytest.mark.asyncio
async def test_derive_whitelist_vector_semantics() -> None:
    """修复：白名单求值保留向量语义——算术/白名单函数/比较/in 可用
    下标/属性/列表推导等一律拒绝且不产出列。"""
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [
                {"type": "derive", "expression": "round(amount * 1.1, 1)", "new_column": "gross"},
                {"type": "derive", "expression": "amount > 150", "new_column": "big"},
            ],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["data"][0]["gross"] == 110.0
    assert result["data"][0]["big"] is False  # 100 > 150 → False
    assert result["data"][2]["gross"] == 330.0
    assert result["data"][2]["big"] is True  # 300 > 150 → True

    blocked = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [
                {"type": "derive", "expression": "[a * 2 for a in amount]", "new_column": "x"},
            ],
        },
        _ctx(),
    )
    assert blocked["success"] is False, "列表推导必须拒绝"


@pytest.mark.asyncio
async def test_derive_unknown_column_fails_clean() -> None:
    """未知列名显式报错（不静默 NaN）。"""
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [{"type": "derive", "expression": "nope * 2", "new_column": "x"}],
        },
        _ctx(),
    )
    assert result["success"] is False
    assert "nope" in str(result.get("error") or "")


@pytest.mark.asyncio
async def test_transform_cast_datetime() -> None:
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": _ROWS,
            "transformations": [{"type": "cast", "column": "date", "target_type": "date"}],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["data"][0]["date"] == "2026-01-05"


@pytest.mark.asyncio
async def test_transform_combine_and_extract() -> None:
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": [{"first": "王", "last": "强", "email_tag": "wq-01"}],
            "transformations": [
                {
                    "type": "combine",
                    "combine_columns": ["first", "last"],
                    "new_column": "full",
                    "combine_separator": " ",
                },
                {
                    "type": "extract",
                    "column": "email_tag",
                    "pattern": r"(\d+)",
                    "new_column": "num",
                },
            ],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["data"][0]["full"] == "王 强"
    assert result["data"][0]["num"] == "01"


# ---------- clean 族 ----------


@pytest.mark.asyncio
async def test_clean_fill_missing_value() -> None:
    _, _, _, clean, *_ = _tools()
    result = await clean.execute(
        {
            "data": _ROWS,
            "operations": [
                {
                    "type": "fill_missing",
                    "columns": ["amount"],
                    "fill_strategy": "value",
                    "fill_value": 0,
                }
            ],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["operations_applied"][0]["values_filled"] == 1
    assert all("amount" in r and r["amount"] is not None for r in result["data"])


@pytest.mark.asyncio
async def test_clean_remove_duplicates() -> None:
    _, _, _, clean, *_ = _tools()
    dup = [{"k": "a"}, {"k": "a"}, {"k": "b"}]
    result = await clean.execute(
        {"data": dup, "operations": [{"type": "remove_duplicates", "columns": ["k"]}]}, _ctx()
    )
    assert result["success"] is True
    assert result["operations_applied"][0]["duplicates_removed"] == 1
    assert result["rows_after"] == 2


@pytest.mark.asyncio
async def test_clean_trim_whitespace() -> None:
    _, _, _, clean, *_ = _tools()
    result = await clean.execute(
        {
            "data": [{"name": "  张伟  "}],
            "operations": [{"type": "trim_whitespace", "columns": ["name"]}],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["data"][0]["name"] == "张伟"


# ---------- validate 族 ----------


@pytest.mark.asyncio
async def test_validate_not_null_flags_null_row() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute(
        {"data": _ROWS, "column_rules": {"amount": [{"type": "not_null"}]}}, _ctx()
    )
    assert result["success"] is True
    assert result["is_valid"] is False
    assert result["total_errors"] == 1
    assert result["invalid_rows_count"] == 1
    assert result["valid_rows_count"] == 3


@pytest.mark.asyncio
async def test_validate_all_valid() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute(
        {"data": _ROWS, "column_rules": {"amount": [{"type": "number"}]}, "row_count": {"gte": 1}},
        _ctx(),
    )
    assert result["success"] is True
    assert result["is_valid"] is True
    assert result["valid_rows_count"] == 4


@pytest.mark.asyncio
async def test_validate_unique_together() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute({"data": _ROWS, "unique_together": [["dept"]]}, _ctx())
    assert result["success"] is True
    assert result["is_valid"] is False
    assert any(e["error"] == "unique_together" for e in result["errors"])


@pytest.mark.asyncio
async def test_validate_required_column_missing() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute(
        {"data": _ROWS, "schema": {"columns": ["dept", "missing_col"]}}, _ctx()
    )
    assert result["success"] is True
    assert result["is_valid"] is False
    assert any(e["error"] == "required_columns" for e in result["errors"])


@pytest.mark.asyncio
async def test_validate_fail_fast_skips_pending_validators() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute(
        {
            "data": _ROWS,
            "column_rules": {"amount": [{"type": "not_null"}]},
            "unique_together": [["dept"]],
            "fail_fast": True,
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["is_valid"] is False
    err_types = {e["error"] for e in result["errors"]}
    assert "not_null" in err_types
    assert "unique_together" not in err_types


# ---------- 空/边缘数据族（DoD-5 真实数据形态） ----------


@pytest.mark.asyncio
async def test_empty_input_aggregate_returns_ok() -> None:
    agg, *_ = _tools()
    result = await agg.execute({"data": []}, _ctx())
    assert result["success"] is True
    assert result["data"] == []


@pytest.mark.asyncio
async def test_validate_empty_data_valid() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute({"data": []}, _ctx())
    assert result["success"] is True
    assert result["is_valid"] is True
    assert result["total_errors"] == 0


# ---------- 装配族（factory 注册 + gate fail-closed，DoD-3 破坏者视角） ----------


def test_data_tools_registered_with_gates() -> None:
    from bandi.agent.factory import build_tool_registry
    from bandi.tools.base import ToolCategory

    registry = build_tool_registry(feature_flags={"data": True, "web": True})
    assert registry.get("data_aggregate").category == ToolCategory.DATA
    assert registry.get("data_aggregate").to_tool_meta()["feature_group"] == "data"
    assert registry.get("data_validate") is not None
    assert registry.get("data_join") is not None  # alias 仍可解析到 tool

    closed = build_tool_registry(feature_flags={"data": False, "web": True})
    for name in ("data_aggregate", "data_clean", "data_merge", "data_transform", "data_validate"):
        assert closed.get(name) is None, f"{name} 应 fail-closed"


def test_data_tools_validate_params_rejects_unknown_tag() -> None:
    agg, *_ = _tools()
    errors = agg.validate_params({"data": [{"a": 1}], "operation": "does_not_exist"})
    assert any("does_not_exist" in e for e in errors)


# ---------- NA/Nullable 序列化族（Bug A/A' 修复契约，DoD-5 真实数据形态） ----------


@pytest.mark.asyncio
async def test_clean_normalize_int_with_null_serializes_none() -> None:
    _, _, _, clean, *_ = _tools()
    result = await clean.execute(
        {
            "data": [{"id": 1}, {"id": None}, {"id": 3}],
            "operations": [{"type": "normalize_types", "type_map": {"id": "int"}}],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert [r["id"] for r in result["data"]] == [1, None, 3]


@pytest.mark.asyncio
async def test_transform_cast_int_with_null_serializes_none() -> None:
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": [{"id": 1}, {"id": None}],
            "transformations": [{"type": "cast", "column": "id", "target_type": "int"}],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert [r["id"] for r in result["data"]] == [1, None]


@pytest.mark.asyncio
async def test_transform_cast_datetime_with_null_no_nat_string() -> None:
    _, _, transform, *_ = _tools()
    result = await transform.execute(
        {
            "data": [{"dt": "2026-01-05"}, {"dt": None}],
            "transformations": [{"type": "cast", "column": "dt", "target_type": "datetime"}],
        },
        _ctx(),
    )
    assert result["success"] is True
    assert result["data"][1]["dt"] is None
    assert "NaT" not in [str(r["dt"]) for r in result["data"]]


@pytest.mark.asyncio
async def test_validate_row_count_malformed_between_fails_gracefully() -> None:
    _, _, _, _, validate = _tools()
    result = await validate.execute({"data": [{"a": 1}], "row_count": {"between": [5]}}, _ctx())
    assert result["success"] is False
    assert "index" in str(result.get("error", "")).lower()
