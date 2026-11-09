# 功能：置信度检测（detect_confidence 从文本/元数据判断置信水平）
#
# 只识别**自指式不确定**（模型对自身回答表达没把握）——普通分析措辞
# （可能/大概/也许/似乎/maybe/perhaps）是研究写作的正常措辞，不得计入
# （否则正常中文分析文被误判低置信 → 澄清循环 → 强制升级）。
from __future__ import annotations

from typing import Final

_UNCERTAINTY_SIGNALS: Final[list[str]] = [
    "不确定",
    "不能确定",
    "无法确定",
    "不肯定",
    "不太好说",
    "无法确认",
    "无法判断",
    "not sure",
    "not certain",
    "cannot determine",
    "can't determine",
    "unable to determine",
]

_PENALTY: Final[float] = 0.1
_FLOOR: Final[float] = 0.1


def detect_confidence(text: str) -> float:
    if not text:
        return 1.0
    lower = text.lower()
    matches = sum(1 for s in _UNCERTAINTY_SIGNALS if s.lower() in lower)
    return max(_FLOOR, 1.0 - matches * _PENALTY)
