# 功能：IntentRouter — 意图提示推断（plan / execute 双模式；结果仅作观测，不切换会话模式）

from __future__ import annotations

from typing import ClassVar

from bandi.core.message import Message


class IntentRouter:
    PLAN_KEYWORDS: ClassVar[list[str]] = ["计划", "规划", "方案", "步骤", "plan", "steps"]

    @staticmethod
    def _has_keyword(content: str, keywords: list[str]) -> bool:
        for kw in keywords:
            if kw.isascii():
                if kw in content.lower():
                    return True
            else:
                if kw in content:
                    return True
        return False

    def route(self, message: Message) -> str:
        """意图提示：命中规划关键词返回 plan，否则 execute 兜底。

        结果仅作 PerceptionResult.intent_hint 观测字段——不得用于切换
        mode_manager 或裁剪工具面（显式会话模式是唯一权威）。
        """
        if not message.content:
            return "execute"
        if self._has_keyword(message.content.strip(), self.PLAN_KEYWORDS):
            return "plan"
        return "execute"
