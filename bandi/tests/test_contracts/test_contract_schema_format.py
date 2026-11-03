"""
契约族：schema format 关键字校验生效（残余 TMC-）。

背景：jsonschema 4.26 的 `FormatChecker()` 默认注册表只有
date/email/idn-email/idn-hostname/ipv4/ipv6/regex/time/uuid——`uri`/`date-time`/
`hostname` 等常用 format 静默不校验（Draft202012 默认 format 仅注解）
审计建议「path 类型」等 format 约束实际未落地。

本契约钉的行为族（生产调用路径）：
- `tools/base.py::_param_validator`（全部工具 validate_params 的校验器工厂）
- 注册后的 Draft202012Validator 实例（iter_errors 走 format_checker）

断言「最终产物」：schema 声明 format: uri / date-time 时，非法值必须产生
校验错误，合法值必须通过——不再是静默放行。
"""

from __future__ import annotations

from bandi.tools.base import BaseTool, ToolCategory


class _FmtTool(BaseTool):
    name = "fmt_probe"
    description = "format 校验探针"
    category = ToolCategory.UTIL

    async def execute(self, params: dict, context=None) -> str:  # type: ignore[override]
        return "ok"


def _tool_with(format_key: str) -> BaseTool:
    cls = type(
        "Probe",
        (BaseTool,),
        {
            "name": "probe",
            "description": "",
            "category": ToolCategory.UTIL,
            "parameters": {
                "type": "object",
                "properties": {"v": {"type": "string", "format": format_key}},
            },
            "execute": lambda self, p, context=None: "ok",
        },
    )
    return cls()


def test_contract_format_uri_invalid_rejected() -> None:
    """format: uri 非法值（含空格）必须被校验拦截。"""
    tool = _tool_with("uri")
    errs = tool.validate_params({"v": "htt p://broken"})
    assert errs, "format: uri 非法值不得静默通过"


def test_contract_format_uri_valid_accepted() -> None:
    """format: uri 合法值必须通过。"""
    tool = _tool_with("uri")
    errs = tool.validate_params({"v": "https://example.com/a?b=1#frag"})
    assert not errs, f"合法 uri 不应报错: {errs}"


def test_contract_format_date_time_invalid_rejected() -> None:
    """format: date-time 非法值必须被校验拦截。"""
    tool = _tool_with("date-time")
    errs = tool.validate_params({"v": "T99:99:99Z"})
    assert errs, "format: date-time 非法值不得静默通过"


def test_contract_format_date_time_valid_accepted() -> None:
    """format: date-time 合法值必须通过。"""
    tool = _tool_with("date-time")
    errs = tool.validate_params({"v": "T12:00:00Z"})
    assert not errs, f"合法 date-time 不应报错: {errs}"


def test_contract_format_hostname_invalid_rejected() -> None:
    """format: hostname 非法值必须被校验拦截。"""
    tool = _tool_with("hostname")
    errs = tool.validate_params({"v": "-bad_host..name"})
    assert errs, "format: hostname 非法值不得静默通过"


def test_contract_format_date_only_rejected() -> None:
    """format: date-time 必须拒绝 date-only（"" 无时间部分，非完整时间戳）。"""
    tool = _tool_with("date-time")
    errs = tool.validate_params({"v": ""})
    assert errs, "date-only 不是合法 date-time，不得放行"


def test_contract_format_path_validated() -> None:
    """format: path 必须校验——空串/NUL 字节拒绝，合法路径放行（五-建议补齐）。"""
    from bandi.tools.base import _FORMAT_CHECKER

    assert _FORMAT_CHECKER.conforms("C:/a/b/c.txt", "path"), "合法绝对路径必须放行"
    assert _FORMAT_CHECKER.conforms("relative/path/x.txt", "path"), "合法相对路径必须放行"
    assert not _FORMAT_CHECKER.conforms("", "path"), "空串不是合法路径"
    assert not _FORMAT_CHECKER.conforms("a\x00b", "path"), "NUL 字节必须拒绝"
    assert not _FORMAT_CHECKER.conforms("   ", "path"), "纯空白不是合法路径"
