# 功能：MCP 动态工具元数据归一化契约（—— 只读/幂等/破坏/并发/风险/缓存/重试 fail-closed）
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from bandi.tools.mcp_client import register_mcp_tools
from bandi.tools.registry import ToolRegistry

_FAIL_CLOSED_DEFAULT = {
    "is_read_only": False,
    "is_idempotent": False,
    "is_destructive": False,
    "is_concurrency_safe": False,
    "risk_level": "medium",
    "cache_ttl": 0.0,
    "retryable": False,
}


def _mcp_client(name: str, tools: list[dict]) -> MagicMock:
    client = MagicMock()
    client.name = name
    client.available = True
    client.list_tools = AsyncMock(return_value=tools)
    return client


def _tool(name: str, annotations: dict | None = None, schema: dict | None = None) -> dict:
    tool: dict = {
        "name": name,
        "description": name,
        "inputSchema": schema if schema is not None else {"type": "object"},
    }
    if annotations is not None:
        tool["annotations"] = annotations
    return tool


async def _build(name: str, tool: dict) -> object:
    registry = ToolRegistry()
    await register_mcp_tools(registry, _mcp_client("srv", [tool]))
    tool_obj = registry.get(f"srv_{name}")
    assert tool_obj is not None, f"工具 srv_{name} 必须注册成功"
    return tool_obj


def _build_sync(name: str, tool: dict) -> object:
    return asyncio.run(_build(name, tool))


def test_contract_mcp_metadata_fail_closed_defaults() -> None:
    tool = _build_sync("plain", _tool("plain"))
    for attr, expected in _FAIL_CLOSED_DEFAULT.items():
        assert getattr(tool, attr) == expected, f"{attr} 未标注时必须 fail-closed={expected}"
    assert tool.risk_level == "medium", "未知风险必须回落到 medium（与既有 MCP 默认一致）"


def test_contract_mcp_metadata_standard_annotations_map() -> None:
    """标准 MCP annotations 必须落到对应元数据：readOnlyHint→只读+并发安全、idempotentHint→幂等。"""
    tool = _build_sync(
        "ro",
        _tool("ro", annotations={"readOnlyHint": True, "idempotentHint": True}),
    )
    assert tool.is_read_only is True
    assert tool.is_idempotent is True
    assert tool.is_concurrency_safe is True, "readOnlyHint 隐式并发安全"

    destructive = _build_sync("del", _tool("del", annotations={"destructiveHint": True}))
    assert destructive.is_destructive is True
    assert destructive.is_read_only is False


def test_contract_mcp_metadata_x_bandi_overrides_annotations() -> None:
    """inputSchema.x-bandi 显式值优先于标准 hints；并携带风险/缓存等私有扩展。"""
    tool = _build_sync(
        "x",
        _tool(
            "x",
            annotations={"readOnlyHint": True},
            schema={
                "type": "object",
                "properties": {},
                "x-bandi": {"read_only": False, "risk": "low", "cache_ttl": 300.0},
            },
        ),
    )
    assert tool.is_read_only is False, "x-bandi 显式 read_only=False 必须覆盖 readOnlyHint"
    assert tool.risk_level == "low"
    assert tool.cache_ttl == 300.0


def test_contract_mcp_metadata_retryable_requires_safe_basis() -> None:
    """retryable=true 必须具备只读或幂等基础，否则 fail-closed 禁重试。"""
    unsafe = _build_sync(
        "r1",
        _tool("r1", schema={"type": "object", "x-bandi": {"retryable": True}}),
    )
    assert unsafe.retryable is False, "非只读/非幂等工具声明 retryable 必须被拒绝"

    safe_read_only = _build_sync(
        "r2",
        _tool("r2", schema={"type": "object", "x-bandi": {"read_only": True, "retryable": True}}),
    )
    assert safe_read_only.retryable is True

    safe_idempotent = _build_sync(
        "r3",
        _tool("r3", schema={"type": "object", "x-bandi": {"idempotent": True, "retryable": True}}),
    )
    assert safe_idempotent.retryable is True


def test_contract_mcp_metadata_malformed_x_bandi_fails_closed() -> None:
    """私有扩展值非法时回落 fail-closed 默认，不得抛异常或带病注册。"""
    tool = _build_sync(
        "bad",
        _tool(
            "bad",
            schema={
                "type": "object",
                "x-bandi": {"risk": "banana", "cache_ttl": -5, "read_only": "yes"},
            },
        ),
    )
    assert tool.risk_level == "medium", "非法风险值必须回落默认 medium"
    assert tool.cache_ttl == 0.0, "负数/非数字 cache_ttl 必须回落 0"
    assert tool.is_read_only is False, "非 bool read_only 必须回落 False"


@pytest.mark.asyncio
async def test_contract_invalid_schema_skipped_siblings_continue() -> None:
    """单个 MCP 工具非法 inputSchema 只跳过该工具并 warning，不得拖垮同批注册。"""
    registry = ToolRegistry()
    client = _mcp_client(
        "srv",
        [
            _tool("good"),
            {"name": "bad", "description": "bad", "inputSchema": {"type": "nope"}},
        ],
    )
    registered = await register_mcp_tools(registry, client)

    assert "srv_good" in registered
    assert "srv_bad" not in registered, "非法 schema 工具必须被跳过"
    assert registry.get("srv_good") is not None
    assert registry.get("srv_bad") is None


@pytest.mark.asyncio
async def test_contract_metadata_applied_through_production_register_path() -> None:
    """生产注册链 register_mcp_tools：最终产物工具对象的元数据必须生效（端到端）。"""
    registry = ToolRegistry()
    client = _mcp_client(
        "srv",
        [
            _tool(
                "fetch",
                annotations={"readOnlyHint": True},
                schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "x-bandi": {"cache_ttl": 60},
                },
            ),
            _tool("mutate", annotations={"destructiveHint": True}),
        ],
    )
    await register_mcp_tools(registry, client)

    fetch = registry.get("srv_fetch")
    mutate = registry.get("srv_mutate")
    assert fetch is not None and mutate is not None
    assert fetch.is_read_only is True
    assert fetch.is_idempotent is False
    assert fetch.is_concurrency_safe is True
    assert fetch.cache_ttl == 60.0
    assert mutate.is_destructive is True
    assert mutate.is_read_only is False


def test_contract_mcp_metadata_v2_fail_closed_defaults() -> None:
    """五维增强字段（TMC-）未标注时必须 fail-closed 默认。"""
    tool = _build_sync("plain2", _tool("plain2"))
    assert getattr(tool, "tool_version", "<missing>") == "", "tool_version 默认必须为空串"
    assert getattr(tool, "output_budget", "<missing>") is None, "output_budget 默认必须 None"
    assert getattr(tool, "cancellable", "<missing>") is False, "cancellable 默认必须 False"
    assert getattr(tool, "needs_context", "<missing>") is False, "needs_context 默认必须 False"
    assert getattr(tool, "provenance", "<missing>") != "<missing>", (
        "provenance 默认必须存在（来源标注）"
    )


def test_contract_mcp_metadata_v2_x_bandi_overrides() -> None:
    """x-bandi 声明五维扩展必须生效。"""
    tool = _build_sync(
        "v2x",
        _tool(
            "v2x",
            schema={
                "type": "object",
                "x-bandi": {
                    "tool_version": "2.1.0",
                    "output_budget": 2048,
                    "cancellable": True,
                    "needs_context": True,
                    "provenance": "mcp:my-server",
                },
            },
        ),
    )
    assert tool.tool_version == "2.1.0"
    assert tool.output_budget == 2048
    assert tool.cancellable is True
    assert tool.needs_context is True
    assert tool.provenance == "mcp:my-server"


def test_contract_mcp_metadata_v2_invalid_values_fail_closed() -> None:
    """非法/越界扩展值回落默认（fail-closed）。"""
    tool = _build_sync(
        "v2bad",
        _tool(
            "v2bad",
            schema={
                "type": "object",
                "x-bandi": {"output_budget": "huge", "cancellable": "yes", "tool_version": 42},
            },
        ),
    )
    assert tool.output_budget is None, "非数值 output_budget 必须回落 None"
    assert tool.cancellable is False, "非布尔 cancellable 必须回落 False"
    assert tool.tool_version == "", "非字符串 tool_version 必须回落空串"


def test_contract_mcp_output_budget_consumed_by_executor() -> None:
    """output_budget 必须被 executor 截断逻辑消费（TMC-接线）。"""
    from bandi.tools.executor import ToolExecutor
    from bandi.tools.registry import ToolRegistry

    tool = _build_sync(
        "budget",
        _tool(
            "budget",
            schema={"type": "object", "x-bandi": {"output_budget": 100}},
        ),
    )
    ex = ToolExecutor(registry=ToolRegistry())
    assert ex._resolve_max_output_tokens("srv_budget", tool) == 100, (
        "output_budget=100 必须作为输出上限被消费"
    )


def test_contract_g2_allowed_tools_whitelist_filters_registration() -> None:
    """-③：allowed_tools 白名单外远端工具不注册（fail-closed）。"""
    import inspect

    from bandi.tools.mcp_client import register_mcp_tools

    sig = inspect.signature(register_mcp_tools)
    assert "allowed_tools" in sig.parameters, "register_mcp_tools 需暴露 allowed_tools 白名单参数"


def test_contract_g2_heuristic_write_hint_forces_confirmation() -> None:
    """-①：名称/描述含写盘动词的远端工具 metadata 强制 risk=high + 非只读
    ——服务器自报缺失/误报时破坏性写盘仍走确认通道。"""
    from bandi.tools.mcp_client import _apply_write_heuristics, _normalize_mcp_metadata

    tool_def = {
        "name": "fs_rename_file",
        "description": "Rename a file on the remote server",
        "inputSchema": {"type": "object", "properties": {}},
        # 服务器未声明任何 hints——归一化后启发式必须兜底
    }
    metadata = _normalize_mcp_metadata(tool_def)
    _apply_write_heuristics(metadata, tool_def["name"], tool_def["description"])
    assert metadata["risk_level"] == "high", "写盘启发式必须强制 high"
    assert metadata["is_read_only"] is False
