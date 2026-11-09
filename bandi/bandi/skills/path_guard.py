# 功能：路径片段安全校验公共件——`skill_id`（SkillCard 落盘）/ `scope_id`（帧栈）/
# 行为约定：非空、长度上限、字符白名单、Windows 保留设备名拦截；失败抛调用方
# 指定的错误类型（保持各族错误语义不变）。
from __future__ import annotations

import re

from bandi.core.exceptions import BandiError

_ASCII_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_UNICODE_SEGMENT_RE = re.compile(r"^[\w\-]+$", re.UNICODE)
DEFAULT_MAX_SEGMENT_LENGTH = 64
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def validate_path_segment(
    value: str,
    *,
    label: str,
    error_cls: type[BandiError],
    ascii_only: bool = False,
    max_length: int = DEFAULT_MAX_SEGMENT_LENGTH,
) -> str:
    """功能：校验路径片段可安全用作文件名（返回原值，失败抛 error_cls）。

    `ascii_only=False`（缺省）允许 Unicode 字母数字（中文会话名/员工名可用）；
    `ascii_only=True` 收紧到 ASCII（技能卡标识来源可为模型输出，取更严口径）。
    """
    pattern = _ASCII_SEGMENT_RE if ascii_only else _UNICODE_SEGMENT_RE
    candidate = str(value or "")
    if len(candidate) > max_length:
        raise error_cls(f"{label} 过长: {candidate!r}")
    if not pattern.fullmatch(candidate):
        raise error_cls(f"{label} 含不安全字符（仅允许字母数字/下划线/连字符）: {candidate!r}")
    if candidate.upper() in _WINDOWS_RESERVED_NAMES:
        raise error_cls(f"{label} 是 Windows 保留设备名: {candidate!r}")
    return candidate
