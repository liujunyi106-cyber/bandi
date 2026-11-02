# 功能：InputParser 单元测试

from __future__ import annotations

import pytest

from bandi.core.message import MessageRole
from bandi.perception.input_parser import InputParser


class TestInputParser:
    def test_parse_normal(self) -> None:
        parser = InputParser()
        msg = parser.parse("你好世界")
        assert msg.role == MessageRole.USER
        assert msg.content == "你好世界"

    def test_parse_strips_whitespace(self) -> None:
        parser = InputParser()
        msg = parser.parse("  带空格的输入  ")
        assert msg.content == "带空格的输入"

    def test_parse_empty_raises(self) -> None:
        parser = InputParser()
        with pytest.raises(ValueError, match="input cannot be empty"):
            parser.parse("")

    def test_parse_whitespace_only_raises(self) -> None:
        parser = InputParser()
        with pytest.raises(ValueError, match="input cannot be empty"):
            parser.parse("   ")
