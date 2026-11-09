# 功能：ContextPlanner — 按 priority 排序 → 按 max_tokens 裁剪

from __future__ import annotations

from bandi.context.item import ContextItem


class ContextPlanner:
    @staticmethod
    def plan(items: list[ContextItem], max_tokens: int) -> list[ContextItem]:
        sorted_items = sorted(items, key=lambda x: x.priority, reverse=True)
        total = sum(i.token_estimate for i in sorted_items)
        if total <= max_tokens:
            return sorted_items
        trimmed: list[ContextItem] = []
        budget = max_tokens
        for item in sorted_items:
            if item.token_estimate <= budget:
                trimmed.append(item)
                budget -= item.token_estimate
        return trimmed
