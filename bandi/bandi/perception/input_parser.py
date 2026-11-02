# 功能：InputParser — 用户原始输入 → Message 对象

from __future__ import annotations

from bandi.core.message import Message, MessageRole


class InputParser:
    def parse(self, raw_input: str) -> Message:
        if not raw_input or not raw_input.strip():
            raise ValueError("input cannot be empty")
        return Message(role=MessageRole.USER, content=raw_input.strip())
