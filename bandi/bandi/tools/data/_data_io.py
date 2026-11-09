# 功能：data 工具族共享 I/O——records 归一化/DataFrame 序列化/结果包装
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import pandas as pd


def as_dataframe(rows: Any) -> pd.DataFrame:
    # 功能：任意 records（list[dict] 或 None）转 DataFrame；None/空 → 空表
    return pd.DataFrame(list(rows) if rows else [])


def _clean_value(value: Any) -> Any:
    # 功能：pandas 标量归一化为可 JSON 序列化的原生 Python（NA/Timestamp/NaN）
    if value is None or value is pd.NA:
        return None
    if isinstance(value, bool):
        return value
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return value
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    return str(value)


def records_out(df: pd.DataFrame) -> list[dict[str, Any]]:
    # 功能：DataFrame → list[dict]，NULL 置 None、时间转 ISO、NaN 归一化
    rows: list[dict[str, Any]] = []
    for rec in df.to_dict(orient="records"):
        rows.append({str(k): _clean_value(v) for k, v in rec.items()})
    return rows


def ok(data: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    # 功能：成功结果包装——data 记录数组 + 统计信息
    return {"success": True, "data": data, **extra}


def fail(error: str) -> dict[str, Any]:
    # 功能：失败结果包装——工具异常不阻断 Agent 循环，统一 success=False
    return {"success": False, "error": error}
