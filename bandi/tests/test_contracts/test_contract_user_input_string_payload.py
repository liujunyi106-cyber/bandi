"""契约：request_user_input 对 JSON 字符串 questions 载荷的宽容解析。

- 背景：（APPR-7）中模型把 questions 数组序列化成字符串提交，
 schema（type: array）校验失败 → 提问卡永不出现（交互通道被堵）。
- 修复语义：schema 接受 array|string；execute 对字符串 json.loads 解析；
 非法 JSON → 退化为空 questions（走既有错误提示路径，不崩）；array 直传不变。"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from bandi.tools.user_input import UserInputTool

QUESTIONS = [{"id": "q1", "question": "选哪个方案?", "choices": ["A", "B"]}]


def _validate_schema(payload: object) -> None:
    Draft202012Validator(UserInputTool(input_fn=lambda _x: "ans").parameters).validate(payload)


class TestUserInputStringPayload:
    def test_schema_accepts_json_string(self) -> None:
        _validate_schema({"questions": json.dumps(QUESTIONS)})

    def test_schema_still_accepts_array(self) -> None:
        _validate_schema({"questions": QUESTIONS})

    @pytest.mark.asyncio
    async def test_execute_parses_json_string(self) -> None:
        tool = UserInputTool(input_fn=lambda _x: "A")
        result = await tool.execute({"questions": json.dumps(QUESTIONS)})
        assert result.get("answers", {}).get("q1") == "A", (
            f"字符串 questions 必须宽容解析并正常提问，实际 {result}"
        )

    @pytest.mark.asyncio
    async def test_execute_invalid_string_degrades_to_hint(self) -> None:
        tool = UserInputTool(input_fn=lambda _x: "A")
        result = await tool.execute({"questions": "{not json"})
        assert "error" in result, "非法字符串必须走显式提示路径（不崩）"

    @pytest.mark.asyncio
    async def test_execute_array_unchanged(self) -> None:
        tool = UserInputTool(input_fn=lambda _x: "B")
        result = await tool.execute({"questions": QUESTIONS})
        assert result.get("answers", {}).get("q1") == "B"
