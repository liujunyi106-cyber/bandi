# 功能：写文件公共基础设施（BOM/编码保真解码 · 外部修改检测 · 原子替换 · per-路径锁）
#
# 既丢 BOM 又把 CRLF 强制成平台行尾）。若两边各写一份，迟早漂移（改一处忘一处）——
# `decode_with_bom` / `check_external_modification` / `atomic_write` / `file_lock`
from __future__ import annotations

import asyncio
import codecs
import hashlib
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from bandi.tools.base import ToolPermissionContext
from bandi.tools.fs_tool import FsTool

FILE_LOCKS_MAX = 512
_FILE_LOCKS: dict[str, asyncio.Lock] = {}

MODIFIED_SINCE_READ = (
    "File has been modified since read, either by the user or by a linter. "
    "Read it again before attempting to write it."
)


def file_lock(target: Path) -> asyncio.Lock:
    # 功能：进程内 per-路径编辑锁（同一文件并发编辑串行化）；（6）：表有界——
    # 超限先清理未持有的条目（防长进程编辑海量路径致 dict 无界增长）
    key = str(target)
    lock = _FILE_LOCKS.get(key)
    if lock is None:
        if len(_FILE_LOCKS) >= FILE_LOCKS_MAX:
            for stale_key in [k for k, v in _FILE_LOCKS.items() if not v.locked()]:
                _FILE_LOCKS.pop(stale_key, None)
                if len(_FILE_LOCKS) < FILE_LOCKS_MAX:
                    break
        lock = asyncio.Lock()
        _FILE_LOCKS[key] = lock
    return lock


def decode_with_bom(raw: bytes) -> tuple[str, str, bytes]:
    # 功能：BOM 显式分离解码——返回 (正文, 编码, BOM 字节)，写回时 BOM 原样加回
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8"), "utf-8", codecs.BOM_UTF8
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw[2:].decode("utf-16-le"), "utf-16-le", codecs.BOM_UTF16_LE
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw[2:].decode("utf-16-be"), "utf-16-be", codecs.BOM_UTF16_BE
    encoding = FsTool.detect_encoding(raw)
    return raw.decode(encoding), encoding, b""


def check_external_modification(
    context: ToolPermissionContext | None, target: Path, raw: bytes
) -> None:
    # 功能：Read 后外部修改检测（mtime + 内容哈希双保险）——有读取记录才启用；
    # 同文件多条记录取 read_at 最新一条为基准；mtime 前进时：partial 读直接拒绝；
    read_state = (getattr(context, "extra", None) or {}).get("read_state")
    if not isinstance(read_state, dict):
        return
    resolved = str(target)
    record: dict[str, Any] | None = None
    latest_read_at = ""
    for value in read_state.values():
        if not isinstance(value, dict) or value.get("resolved_path") != resolved:
            continue
        read_at = value.get("read_at")
        if not isinstance(read_at, str):
            read_at = ""
        if record is None or read_at >= latest_read_at:
            record = value
            latest_read_at = read_at
    if record is None:
        return
    recorded_mtime = record.get("mtime")
    if not isinstance(recorded_mtime, (int, float)):
        return
    try:
        current_mtime = target.stat().st_mtime
    except OSError:
        return
    if current_mtime <= recorded_mtime:
        return
    if record.get("partial"):
        raise ValueError(MODIFIED_SINCE_READ)
    recorded_digest = record.get("content_sha256")
    if isinstance(recorded_digest, str) and recorded_digest == hashlib.sha256(raw).hexdigest():
        return
    raise ValueError(MODIFIED_SINCE_READ)


def atomic_write(target: Path, data: bytes) -> None:
    # 功能：临时文件 + fsync + os.replace 原子替换（崩溃不留半截；
    # 失败清理临时文件）
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}-{uuid4().hex[:8]}")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except (OSError, ValueError):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
