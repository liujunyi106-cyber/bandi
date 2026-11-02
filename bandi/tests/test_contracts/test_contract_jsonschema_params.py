# 功能：工具参数浅层校验升级契约（—— jsonschema 深层约束 + 注册门禁 fail-closed）
import pytest

from bandi.tools.base import BaseTool, ToolCategory


def _make_tool(tool_name: str, parameters: dict) -> type[BaseTool]:
    params = parameters

    class _Tool(BaseTool):
        name = tool_name
        description = tool_name
        category = ToolCategory.UTIL
        parameters = params

        async def execute(self, params, context=None):  # type: ignore[override]
            return "ok"

    return _Tool


def test_contract_validate_params_applies_nested_and_type_constraints() -> None:
    """
    浅层手工校验只查顶层类型，pattern/minLength/maxItems/嵌套 properties 一律漏检；
    升级 jsonschema 后这些必须全部拦截——契约钉深层约束行为族。"""
    tool = _make_tool(
        "deep_op",
        {
            "type": "object",
            "properties": {
                "code": {"type": "string", "pattern": "^[0-9]{3}$"},
                "tags": {"type": "array", "items": {"type": "integer"}, "maxItems": 2},
                "nested": {
                    "type": "object",
                    "properties": {"level": {"type": "integer", "minimum": 1}},
                    "required": ["level"],
                },
            },
            "required": ["code"],
        },
    )()

    assert tool.validate_params({"code": "a12"}), "pattern 不匹配必须拦截（浅层校验漏检）"
    assert tool.validate_params({"code": "123", "tags": [1, 2, 3]}), "maxItems 超限必须拦截"
    assert tool.validate_params({"code": "123", "tags": ["x"]}), "items 类型不符必须拦截"
    assert tool.validate_params({"code": "123", "nested": {"level": 0}}), "minimum 越界必须拦截"
    assert tool.validate_params({"code": "123", "nested": {}}), "缺失嵌套必填必须拦截"


def test_contract_validate_params_rejects_extra_and_type_mismatch() -> None:
    """jsonschema 替换不得放宽既有语义：boolean 字段拒非 bool、type 错配必拒。"""
    tool = _make_tool(
        "typed_op",
        {
            "type": "object",
            "properties": {"flag": {"type": "boolean"}, "n": {"type": "number"}},
            "additionalProperties": False,
        },
    )()

    assert tool.validate_params({"flag": 1}), "boolean 字段不得接受 int"
    assert tool.validate_params({"n": "x"}), "number 字段不得接受 str"
    assert tool.validate_params({"surprise": True}), "additionalProperties=false 必拒未知键"


def test_contract_validate_params_accepts_valid_input() -> None:
    """合法输入必须零错误返回——替换实现不得误杀正常调用。"""
    tool = _make_tool(
        "ok_op",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["a", "b"]},
                "count": {"type": "integer", "minimum": 0},
            },
            "required": ["mode"],
        },
    )()
    assert tool.validate_params({"mode": "a", "count": 3}) == []
    assert tool.validate_params({"mode": "b"}) == []


def test_contract_tool_registration_rejects_invalid_schema_fail_closed() -> None:
    """
    非法参数 schema（type 值无效 / required 非数组）必须在注册入口直接拒绝（ValueError）
    不得带病注册后运行时才崩溃——fail-closed。"""
    from bandi.tools.registry import ToolRegistry

    for bad in ({"type": "nope"}, {"type": "object", "required": "notalist"}):
        reg = ToolRegistry()
        tool_cls = _make_tool("bad_op", bad)
        with pytest.raises(ValueError):
            reg.register(tool_cls)
        assert reg.get("bad_op") is None, "非法 schema 必须 fail-closed，不得留下半注册工具"

        reg2 = ToolRegistry()
        with pytest.raises(ValueError):
            reg2.register_instance(tool_cls())
        assert reg2.get("bad_op") is None


def test_contract_tool_registration_accepts_empty_and_extended_schema() -> None:
    """
    注册门禁不得误杀：空参数（MCP inputSchema:{}）与带自定义扩展关键字（x- 标注）
    的 schema 必须可注册——校验器对未知关键字本就忽略。"""
    from bandi.tools.registry import ToolRegistry

    for parameters in (
        {},
        {"type": "object", "properties": {}},
        {
            "type": "object",
            "x-bandi": {"read_only": True},
            "properties": {"a": {"type": "string"}},
        },
    ):
        reg = ToolRegistry()
        reg.register(_make_tool(f"ok_{len(parameters)}", parameters))
        assert reg.get(f"ok_{len(parameters)}") is not None
