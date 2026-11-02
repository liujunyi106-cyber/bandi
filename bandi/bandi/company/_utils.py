# 功能：company 层共享辅助（Mapping 浅拷贝 / 宽松 int 解析 / 路径安全校验）

"""company 层跨文件重复辅助的公共宿主。

阶段 E DRY 复扫提取：
- ``as_mapping``：prompt_contract / work_item_identity / turn_mode 重复（等价）。
- ``as_int``：turn_mode / reactivation_sweeper / work_item_plan_types 重复（后者带 default 变体）。
统一签名 ``as_int(value, default=0)`` 语义与原三版完全兼容。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


def as_mapping(value: Any) -> dict[str, Any]:
    """Return a shallow copy when value is a mapping, else {}."""
    return dict(value) if isinstance(value, Mapping) else {}


def as_int(value: Any, default: int = 0) -> int:
    """宽松 int 解析：None 或解析失败回落 default，其余走 int()（容忍 0/0.0/带引号数字）。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def path_within(candidate: Path, root: Path) -> bool:
    """判断 candidate 是否在 root 内部（跨盘符安全回退）。"""
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        resolved = str(candidate.expanduser().resolve(strict=False))
        root_value = str(root.expanduser().resolve(strict=False))
        # 按已解析字符串实际含的分隔符归一（"\\" 优先，POSIX 回退 "/"）
        root_norm = root_value.rstrip("/\\")
        sep = "\\" if "\\" in resolved else "/"
        return resolved == root_norm or resolved.startswith(root_norm + sep)
