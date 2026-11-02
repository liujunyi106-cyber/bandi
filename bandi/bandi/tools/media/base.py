# 功能：媒体生成工具族基础（文件名派生/落盘统一）
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from bandi.generation.base import GenerationOutput, ext_for_kind
from bandi.tools.base import ToolPermissionContext

_WEAK_WORDS = frozenset(
    {
        "image",
        "photo",
        "picture",
        "video",
        "clip",
        "audio",
        "sound",
        "generate",
        "生成",
        "图片",
        "视频",
        "音频",
    }
)


def _stem_from_prompt(prompt: str, kind: str, max_len: int = 48) -> str:
    # 过滤掉噪音词（弱 token 只保留第一个离词，保证仍有意义），再哈希截断以避免超长/重复文件名
    tokens = [t for t in re.split(r"[\W_]+", prompt or "") if t and t.lower() not in _WEAK_WORDS]
    for word in ("image", "photo", "picture", "video", "clip", "audio", "sound"):
        if not tokens and word == kind:
            tokens.append(kind)
            break
    if not tokens:
        tokens = ["asset"]
    stem_pre = "_".join(tokens)[:max_len]
    suffix = hashlib.sha256((prompt or "asset").encode("utf-8")).hexdigest()[:8]
    return f"{stem_pre}_{suffix}"


def build_media_filename(prompt: str, kind: str) -> str:
    """从提示词派生稳定的落盘文件名（<stem>.<ext>）。"""
    return f"{_stem_from_prompt(prompt, kind)}.{ext_for_kind(kind)}"


def media_project_root(context: ToolPermissionContext | None) -> Path:
    """工具执行的工作区根：优先 ctx 注入的 project_root，否则全局 workspace。"""
    from bandi.tools.fs_tool import project_base, workspace_root

    base = project_base(context)
    return Path(base) if base else workspace_root()


def media_default_output_path(prompt: str, kind: str, context: ToolPermissionContext | None) -> str:
    """默认落盘：workspace/generated/<kind>/<stem>.<ext>。"""
    root = media_project_root(context)
    return str(root / "generated" / kind / build_media_filename(prompt, kind))


async def save_generation_output(
    output: GenerationOutput,
    *,
    output_path: str,
    project_root: str,
) -> tuple[bool, str, str]:
    """把生成产物写入指定路径（越界即拒绝）。

    返回 (success, error|'', actual_saved_path)；actual_saved_path 为按 project_root
    解析后的绝对落盘路径（相对 output_path 不依赖进程 CWD），失败时为空串。
    """
    root = Path(project_root).resolve()
    if output.data is None:
        return False, "generation returned no data", ""
    if not output_path:
        return False, "output_path is required", ""
    target = Path(output_path)
    if not target.is_absolute():
        target = root / target
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        return False, f"output path is outside workspace: {target}", ""
    from bandi.tools.fs_tool import sensitive_path_reason

    reason = sensitive_path_reason(resolved)
    if reason:
        return False, reason, ""
    resolved.parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved.write_bytes(output.data)
    except OSError as e:
        return False, f"write failed: {e}", ""
    return True, "", str(resolved)


__all__ = [
    "build_media_filename",
    "media_default_output_path",
    "media_project_root",
    "save_generation_output",
]
