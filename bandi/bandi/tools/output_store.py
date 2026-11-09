# 功能：工具结果全文落盘——压缩/裁剪前保存原文并返回可读路径
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

_OUTPUT_DIR_ENV = "BANDI_TOOL_OUTPUT_DIR"
_DEFAULT_SUBDIR = ("data", "tool_outputs")
_MAX_NAME_CHARS = 40


def tool_output_root() -> Path:
    """功能：工具输出落盘根目录（环境变量可覆盖，测试隔离用）"""
    override = os.environ.get(_OUTPUT_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home().joinpath(".bandi", *_DEFAULT_SUBDIR)


def _safe_tool_name(tool_name: str) -> str:
    raw = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in (tool_name or "tool"))
    return raw[:_MAX_NAME_CHARS] or "tool"


def persist_tool_content(tool_name: str, content: str, *, prefix: str = "pruned") -> str | None:
    """功能：把工具结果全文原子落盘；失败返回 None（调用方保留原内容）"""
    text = content or ""
    if not text:
        return None
    root = tool_output_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        name = f"{prefix}_{_safe_tool_name(tool_name)}_{stamp}_{uuid.uuid4().hex[:8]}.txt"
        target = root / name
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return str(target)
    except OSError:
        return None
