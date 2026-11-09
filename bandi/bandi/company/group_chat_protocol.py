# 功能：群聊发言协议——PASS 判定、提及拒绝与统一提示
from __future__ import annotations

import re

from bandi.core.exceptions import BandiError

MENTION_DEPRECATED = "MENTION_DEPRECATED"
MENTION_NOT_IN_PROJECT = "MENTION_NOT_IN_PROJECT"

GROUP_PASS_HINT = (
    "群聊规则：只回复你这一轮要补充的内容；如果本轮没有新的补充，"
    "只回复 [PASS]，不要重复上一位成员。"
    "弃权时不要加标点、解释或补充内容。"
)

_PASS_CORE = r"\s*pass\s*"
_PASS_RE = re.compile(
    rf"^(?:{_PASS_CORE}|[\[（(【]{_PASS_CORE}[\]）)】])\s*[。.!！?？]?\s*$",
    re.IGNORECASE,
)
_PASS_MARKS = ("**", "__", "`")


def _normalize_pass_text(text: str) -> str:
    value = str(text or "").replace("\u200b", "").replace("\ufeff", "").strip()
    for mark in _PASS_MARKS:
        if len(value) > len(mark) * 2 and value.startswith(mark) and value.endswith(mark):
            value = value[len(mark) : -len(mark)].strip()
    return value


class MentionError(BandiError):
    """功能：群聊提及被拒绝——废弃广播词或提及了不在当前项目的员工。"""

    def __init__(self, code: str, mentions: list[str], message: str = "") -> None:
        self.code = code
        self.mentions = list(mentions)
        super().__init__(message or f"{code}: {', '.join(self.mentions)}")


def mention_error_text(error: MentionError) -> str:
    """功能：提及拒绝的用户可见文案（WS/CLI 同源）。"""
    if error.code == MENTION_DEPRECATED:
        return "@all / @所有人 已废弃，请 @ 具体员工。"
    if error.code == MENTION_NOT_IN_PROJECT:
        names = "、".join(f"@{name}" for name in error.mentions)
        return f"{names} 不在当前项目，请先把该员工拉入项目后再 @。"
    return str(error)


def is_pass_output(text: str) -> bool:
    """功能：判断群聊成员本轮是否选择沉默"""
    return bool(_PASS_RE.match(_normalize_pass_text(text)))
