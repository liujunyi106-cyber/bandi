"""契约：emit_ui_tree 对 JSON 字符串载荷的宽容解析（模型真实行为）。

- 背景：真机（APPR-7 群聊 plan）中模型把组件树序列化成 JSON 字符串提交，
  schema（type: object）直接校验失败 → 员工连接 3 次失败 → 15 轮 MAX_TURNS。
- 修复语义：schema 接受 object|string；execute 对字符串做 json.loads 宽容解析，
  非法 JSON 显式报错（不静默）；object 直传原路径不变。
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from bandi.tools.canvas.ui_components import EmitUiTreeTool


def _validate_schema(payload: object) -> None:
    Draft202012Validator(EmitUiTreeTool().parameters).validate(payload)


TREE = {"kind": "DesignSurface", "props": {"preset": "card"}, "children": []}


class TestEmitUiTreeStringPayload:
    def test_schema_accepts_json_string_payload(self) -> None:
        _validate_schema({"tree": json.dumps(TREE)})

    def test_schema_still_accepts_object_payload(self) -> None:
        _validate_schema({"tree": TREE})

    @pytest.mark.asyncio
    async def test_execute_parses_json_string(self) -> None:
        tool = EmitUiTreeTool()
        result = await tool.execute({"tree": json.dumps(TREE)})
        assert result["success"] is True, f"字符串载荷必须宽容解析，实际 {result}"
        assert result["tree"]["kind"] == "DesignSurface"

    @pytest.mark.asyncio
    async def test_execute_object_unchanged(self) -> None:
        tool = EmitUiTreeTool()
        result = await tool.execute({"tree": TREE})
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_execute_invalid_string_reports_explicit_error(self) -> None:
        tool = EmitUiTreeTool()
        result = await tool.execute({"tree": "{not json"})
        assert result["success"] is False
        assert "JSON" in str(result["error"])
