# 功能：SQLite 连接统一入口——收敛散落的 PRAGMA 与连接参数口径（WAL / busy_timeout / 线程安全 / 外键）

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# sqlite3.connect 默认 timeout（秒）。**注意**：CPython 把该参数直接映射为
# sqlite3_busy_timeout(timeout * 1000)，即 `timeout` 本身就是写等待上限。
DEFAULT_TIMEOUT_SECONDS = 5.0

# 数据库文件期望权限（仅 POSIX 生效；Windows 见 secure_db_file）。
DB_FILE_MODE = 0o600

_MEMORY_MARKER = ":memory:"


def _is_memory(path: str | Path) -> bool:
    return str(path) == _MEMORY_MARKER


def _apply_pragmas(
    conn: sqlite3.Connection,
    *,
    wal: bool,
    foreign_keys: bool,
    query_only: bool,
    busy_timeout_ms: int | None,
    hardened: bool = False,
) -> None:
    """按固定顺序施加 PRAGMA。

    顺序有讲究：journal_mode 必须在任何事务之外设置——sqlite3 的 PRAGMA 不是
    DML，在 defer（默认）与 autocommit 两种 isolation_level 下都不会隐式开事务。
    """
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout_ms is not None:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if foreign_keys:
        conn.execute("PRAGMA foreign_keys=ON")
    if query_only:
        conn.execute("PRAGMA query_only=ON")
    if hardened:
        # 应用自有库纵深防御：删除即擦除 + 关闭 schema 内嵌函数信任
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("PRAGMA trusted_schema=OFF")
    # 刻意不改 synchronous（保持 stdlib 默认 FULL）——WAL+NORMAL 虽是常见性能档


def connect(
    path: str | Path,
    *,
    wal: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    check_same_thread: bool = True,
    isolation_level: str | None = "",
    row_factory: bool = False,
    foreign_keys: bool = False,
    query_only: bool = False,
    busy_timeout_ms: int | None = None,
    hardened: bool = False,
) -> sqlite3.Connection:
    """统一 SQLite 连接入口。

    参数默认值与 stdlib `sqlite3.connect` **完全一致**，因此调用方只需替换函数名
    行为零变化；差异仅在显式打开的可选项上。

    Args:
        path: 数据库路径或 ":memory:"。
        wal: 是否启用 WAL 日志模式。**仅对应用自有库开启**——WAL 是写进库文件头部的
            持久化设置，对外部/用户数据源开启等于擅自修改用户文件。
        timeout: 写锁等待秒数（等价 busy_timeout = timeout * 1000）。
        check_same_thread: 是否允许跨线程复用连接。多线程/异步存储须置 False。
        isolation_level: 事务模式。"" = defer（sqlite3 默认）；None = autocommit。
        row_factory: 是否启用 sqlite3.Row 行工厂。
        foreign_keys: 是否开启外键约束。**默认关**——对存量库开启可能暴露既有孤儿行。
        query_only: 是否只读（用于分析型/外部数据源）。
        busy_timeout_ms: 显式写等待上限（毫秒）。默认 None = 沿用 timeout 推导值
            仅在有特殊审计需求时显式指定。
    """
    conn = sqlite3.connect(
        str(path),
        timeout=timeout,
        check_same_thread=check_same_thread,
        isolation_level=isolation_level,
    )
    _apply_pragmas(
        conn,
        wal=wal,
        foreign_keys=foreign_keys,
        query_only=query_only,
        busy_timeout_ms=busy_timeout_ms,
        hardened=hardened,
    )
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def connect_app_db(
    path: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    isolation_level: str | None = "",
    row_factory: bool = False,
    foreign_keys: bool = False,
) -> sqlite3.Connection:
    """应用自有存储（`~/.bandi/data/*.db`）的连接：WAL + 跨线程安全。

    `row_factory` / `foreign_keys` 保持显式——不同 store 的存量行为不一致
    统一改会引入非预期差异（例如 tuple 与 Row 的相等性比较语义不同）。
    """
    return connect(
        path,
        wal=True,
        timeout=timeout,
        check_same_thread=False,
        isolation_level=isolation_level,
        row_factory=row_factory,
        foreign_keys=foreign_keys,
        hardened=True,
    )


def secure_db_file(path: str | Path) -> None:
    """尽力把数据库文件（含 WAL 旁路文件）权限收紧到 0600。

    ⚠️ 平台差异（已在计划中登记）：POSIX 下 `os.chmod` 直接生效；**Windows 下
    `os.chmod` 只影响只读位，不改变 ACL**，本函数在该平台为 no-op。Windows 上的
    真实隔离依赖用户目录 ACL，不属于本函数职责。
    """
    if _is_memory(path) or os.name == "nt":
        return
    base = Path(path)
    for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
        if not candidate.exists():
            continue
        try:
            os.chmod(candidate, DB_FILE_MODE)
        except OSError:
            # 权限收紧是加固项而非功能项——失败不得影响主流程。
            pass
