# 功能：db 工具族共享库——路径沙箱/连接工厂/单语句校验/破坏性门控/参数归一/结果包装
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from bandi.datasource.adapters.base_sql import _split_statements
from bandi.utils.db import connect as _db_connect

DEFAULT_DATA_ROOT = Path(os.environ.get("BANDI_DATA_DIR") or "~/.bandi/data").expanduser()

_DESTRUCTIVE_RE = re.compile(
    r"\b(DROP|TRUNCATE)\b|\bALTER\s+TABLE\b.*\bDROP\b",
    re.IGNORECASE | re.DOTALL,
)
_FORBIDDEN_ATTACH_RE = re.compile(r"\b(ATTACH|DETACH)\b", re.IGNORECASE)


def resolve_db_path(db_path: str, data_root: str) -> Path:
    # 功能：绝对化 + resolve 后必须在 data_root 沙箱内（symlink 逃逸 resolve 后拦截）
    raw = Path(db_path)
    if not raw.is_absolute():
        raise ValueError("db_path must be an absolute path")
    root = Path(data_root).resolve()
    target = raw.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"db_path escapes the data sandbox: {target}") from None
    return target


def connect(db_path: str, data_root: str, timeout_seconds: int = 30) -> sqlite3.Connection:
    # 功能：打开沙箱内 sqlite 库——row_factory=Row，短连接所有权归调用方
    target = resolve_db_path(db_path, data_root)
    # 沙箱内工作区库：只统一写等待口径，不切 WAL
    # （避免在用户工作区产生 -wal/-shm 旁路文件）。
    return _db_connect(target, timeout=float(timeout_seconds), row_factory=True)


def coerce_params(raw: Any) -> dict[str, Any]:
    # 功能：命名参数归一——dict 直通；list 转 p0/p1...；None → 空
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (list, tuple)):
        return {f"p{i}": v for i, v in enumerate(raw)}
    raise TypeError("params must be an object or array")


def assert_single_statement(sql: str) -> str:
    # 功能：分号感知分段后必须恰 1 条——多语句直接拒绝（防 `SELECT 1; DROP` 整条绕过）
    statements = [s for s in _split_statements(sql) if s.strip()]
    if len(statements) != 1:
        raise ValueError("Multiple SQL statements are not allowed; run one statement at a time")
    stmt = statements[0].strip().rstrip(";").strip()
    if not stmt:
        raise ValueError("SQL statement is empty")
    return stmt


def assert_no_attach(sql: str) -> None:
    # 功能：禁 ATTACH/DETACH——attach 其他 slite 文件会绕过沙箱边界
    if _FORBIDDEN_ATTACH_RE.search(sql):
        raise ValueError("ATTACH/DETACH is not allowed")


def is_destructive(sql: str) -> bool:
    # 功能：DROP/TRUNCATE/ALTER...DROP 识别
    return bool(_DESTRUCTIVE_RE.search(sql))


def ok(**extra: Any) -> dict[str, Any]:
    # 功能：成功结果包装——success=True + 业务字段
    return {"success": True, **extra}


def fail(error: str) -> dict[str, Any]:
    # 功能：失败结果包装——工具异常不阻断 Agent 循环，统一 success=False
    return {"success": False, "error": error}
