# 功能：时间序列化唯一出口——接口层输出带本地偏移的 ISO 串，存储层写入 UTC 带偏移串

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

# 历史本地 naive 串：`YYYY-MM-DDTHH:MM:SS[.ffffff]`（无偏移后缀）
_NAIVE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?$")

# 时间串形状（SQL 侧粗筛，避免全表扫描非时间列值）；空格分隔的历史串一并覆盖
_TIME_SHAPE_LIKE = "____-__-__T__:__:__%"
_TIME_SHAPE_LIKE_SPACE = "____-__-__ __:__:__%"


def iso_local(value: object) -> str:
    """功能：时间值 → 带本地时区偏移的 ISO 串。

    naive 值按系统本地时间解释（历史存储口径），aware 值按其偏移换算；
    字符串先按 ISO 解析再归一（解析失败原样返回，不吞数据）；空值返回空串。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        resolved = value if value.tzinfo is not None else value.astimezone()
        return resolved.astimezone().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            return iso_local(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return text
    return str(value)


def to_utc_iso(text: str) -> str:
    """功能：时间串归一为 UTC 带偏移 ISO 串；naive 按本地时区解释，解析失败原样返回。"""
    try:
        parsed = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return str(text)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC).isoformat()


def migrate_naive_timestamps(
    conn: sqlite3.Connection,
    table_columns: dict[str, tuple[str, ...]],
) -> int:
    """功能：把历史本地 naive 时间串改写为 UTC 带偏移串（幂等，仅改 naive 值）。

    逐表逐列按时间串形状粗筛，正则确认无偏移后按本地时区换算。已带偏移的值跳过，
    因此重复调用无副作用；返回实际改写格数供日志观测。
    """
    updated = 0
    for table, columns in table_columns.items():
        for column in columns:
            try:
                rows: list[Any] = conn.execute(
                    f"SELECT rowid, {column} FROM {table} "
                    f"WHERE {column} IS NOT NULL AND {column} != '' "
                    f"AND ({column} LIKE ? OR {column} LIKE ?)",
                    (_TIME_SHAPE_LIKE, _TIME_SHAPE_LIKE_SPACE),
                ).fetchall()
            except sqlite3.OperationalError:
                # 表/列不存在（旧库缺列）——跳过该列
                continue
            for row in rows:
                rowid, value = row[0], row[1]
                text = str(value)
                if not _NAIVE_ISO_RE.match(text):
                    continue
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE rowid = ?",
                    (to_utc_iso(text), rowid),
                )
                updated += 1
    return updated
