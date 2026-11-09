# 功能：内容安全过滤 — 基于关键词类别匹配，规则从默认列表加载（支持 YAML 配置）

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bandi.core.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """内容过滤结果。"""

    blocked: bool
    categories: dict[str, list[str]] = field(default_factory=dict)
    action: str = "pass"  # "block" | "warn" | "pass"


DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "violence": [
        "恐怖袭击",
        "自杀方法",
        "制造炸弹",
        "砍死",
        "杀掉",
        "枪击",
        "爆炸物",
        "核武器",
        "生化武器",
        "bomb",
        "kill yourself",
        "shoot",
        "murder",
        "terrorist",
    ],
    "hate": [
        "种族灭绝",
        "去死",
        "支那",
        "日本鬼子",
        "韩国猪",
        "女性就该",
        "残障人士活该",
        "racial",
        "genocide",
        "nazi",
        "white supremacy",
    ],
    "self_harm": [
        "自杀",
        "不想活了",
        "自我了断",
        "割腕",
        "跳楼方法",
        "suicide",
        "self-harm",
        "cut myself",
        "end my life",
    ],
    "illegal": [
        "黑客攻击",
        "盗号",
        "走私",
        "制毒",
        "洗钱",
        "盗版网站",
        "破解软件",
        "伪造证件",
        "hack into",
        "crack software",
        "drug synthesis",
        "money laundering",
    ],
}


class ContentFilter:
    """

    检测点：Agent 最终输出后、返回给用户之前。
    """

    def __init__(
        self, categories: dict[str, list[str]] | None = None, event_bus: EventBus | None = None
    ) -> None:
        self._categories = DEFAULT_CATEGORIES if categories is None else categories
        self._event_bus = event_bus

    def check(self, text: str) -> FilterResult:
        """按类别逐模式匹配，返回拦截决策。"""
        matched: dict[str, list[str]] = {}
        for category, keywords in self._categories.items():
            found: list[str] = []
            for keyword in keywords:
                if self._keyword_in_text(text, keyword):
                    found.append(keyword)
            if found:
                matched[category] = found
        blocked = len(matched) > 0
        if blocked:
            self._emit_safety_event(matched)
        return FilterResult(
            blocked=blocked,
            categories=matched,
            action="block" if blocked else "pass",
        )

    @staticmethod
    def _keyword_in_text(text: str, keyword: str) -> bool:
        # `shoot` 不得匹配 `shooter`（子串匹配误拦截）；
        # 中文关键词无词边界概念，保持子串匹配
        if any(ch.isascii() and ch.isalpha() for ch in keyword):
            pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])"
            return re.search(pattern, text, re.IGNORECASE) is not None
        return keyword in text

    def _emit_safety_event(self, matched: dict[str, list[str]]) -> None:
        if self._event_bus is None:
            return
        try:
            import asyncio

            from bandi.core.events import Event, EventType

            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self._event_bus.emit(
                        Event(
                            type=EventType.SAFETY_GUARD_TRIGGERED,
                            data={
                                "module": "content_filter",
                                "action": "block",
                                "categories": {k: v for k, v in matched.items()},
                            },
                        )
                    )
                )
        except RuntimeError:
            logger.warning("Failed to emit safety event: no running event loop")
        except ImportError:
            logger.warning("Failed to emit safety event: event types not available")
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to emit safety event: %s", e)
