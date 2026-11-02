from __future__ import annotations

from types import SimpleNamespace

import pytest

from bandi.core.message import ToolCall, ToolResult
from bandi.tools.middleware.ownership_mw import OwnershipMiddleware
from bandi.tools.pipeline import ToolContext


def _work_item(**kwargs):
    metadata = kwargs.pop("metadata", {})
    return SimpleNamespace(
        id=kwargs.pop("id", "wi-1"),
        assignee_role=kwargs.pop("assignee_role", "worker"),
        metadata=metadata,
    )


def _call(name: str, arguments: dict) -> ToolCall:
    return ToolCall(id="call-1", name=name, arguments=arguments)


@pytest.mark.asyncio
async def test_rejects_without_work_item():
    mw = OwnershipMiddleware()
    ctx = ToolContext()
    result = await mw.before_execute(ctx, _call("file_edit", {"path": "/tmp/x.txt"}))
    assert not result.allow
    assert "work_item not available" in result.reason


@pytest.mark.asyncio
async def test_passes_for_read_tool():
    mw = OwnershipMiddleware()
    wi = _work_item(
        metadata={
            "execution_mode": "company_mode",
            "ownership_contract": {"write_scope": "read_only"},
        }
    )
    ctx = ToolContext(extra={"work_item": wi})
    result = await mw.before_execute(ctx, _call("web_search", {}))
    assert result.allow


@pytest.mark.asyncio
async def test_blocks_write_when_read_only():
    mw = OwnershipMiddleware()
    wi = _work_item(
        metadata={
            "execution_mode": "company_mode",
            "ownership_contract": {"write_scope": "read_only"},
        }
    )
    ctx = ToolContext(extra={"work_item": wi})
    result = await mw.before_execute(ctx, _call("file_edit", {"path": "/tmp/x.txt"}))
    assert not result.allow
    assert "read_only" in (result.reason or "")


@pytest.mark.asyncio
async def test_blocks_collaboration_to_unknown_recipient():
    mw = OwnershipMiddleware()
    wi = _work_item(
        metadata={
            "execution_mode": "company_mode",
            "ownership_contract": {"allowed_collaboration_targets": ["manager"]},
        }
    )
    ctx = ToolContext(extra={"work_item": wi})
    result = await mw.before_execute(ctx, _call("send_dm", {"to_agent": "stranger"}))
    assert not result.allow
    assert "not allowed" in (result.reason or "")


@pytest.mark.asyncio
async def test_allows_collaboration_to_known_recipient():
    mw = OwnershipMiddleware()
    wi = _work_item(
        metadata={
            "execution_mode": "company_mode",
            "ownership_contract": {"allowed_collaboration_targets": ["manager"]},
        }
    )
    ctx = ToolContext(extra={"work_item": wi})
    result = await mw.before_execute(ctx, _call("send_dm", {"to_agent": "manager"}))
    assert result.allow


@pytest.mark.asyncio
async def test_after_execute_passthrough():
    mw = OwnershipMiddleware()
    result = ToolResult(tool_call_id="call-1", tool_name="file_edit", success=True, output="done")
    ctx = ToolContext()
    out = await mw.after_execute(ctx, _call("file_edit", {}), result)
    assert out is result


def _acquisition_item(**kwargs):
    return _work_item(
        assignee_role="acquisition_specialist",
        metadata={
            "work_item_projection_id": "data_acquisition",
            "workspace_manifest": {"root_path": "/workspace/wi-1"},
            **kwargs.pop("metadata", {}),
        },
        **kwargs,
    )


@pytest.mark.asyncio
async def test_blocks_acquisition_output_outside_workspace():
    mw = OwnershipMiddleware()
    ctx = ToolContext(extra={"work_item": _acquisition_item()})
    result = await mw.before_execute(
        ctx,
        _call(
            "bash",
            {
                "command": "curl -o /etc/evil.sh http://example.com/v.mp4",
                "working_directory": "/workspace/wi-1",
            },
        ),
    )
    assert not result.allow


@pytest.mark.asyncio
async def test_allows_acquisition_output_in_workspace():
    mw = OwnershipMiddleware()
    ctx = ToolContext(extra={"work_item": _acquisition_item()})
    result = await mw.before_execute(
        ctx,
        _call(
            "bash",
            {
                "command": "curl -o a.mp4 http://example.com/v.mp4",
                "working_directory": "/workspace/wi-1",
            },
        ),
    )
    assert result.allow


@pytest.mark.asyncio
async def test_non_acquisition_shell_allowed():
    mw = OwnershipMiddleware()
    ctx = ToolContext(extra={"work_item": _acquisition_item()})
    result = await mw.before_execute(
        ctx,
        _call("bash", {"command": "ls -la", "working_directory": "/workspace/wi-1"}),
    )
    assert result.allow


@pytest.mark.asyncio
async def test_acquisition_guard_reads_cwd_param():
    """
    working_directory 恒空，cwd 在投影外时相对输出路径按 target_output_dir 解析
    误判在范围内放行（实际写到 cwd 范围外文件，泄漏）。"""
    mw = OwnershipMiddleware()
    ctx = ToolContext(extra={"work_item": _acquisition_item()})
    result = await mw.before_execute(
        ctx,
        _call(
            "bash",
            # → 误放行；新实现必须按真实 cwd 解析 → 拦截
            {"command": "curl -o a.mp4 http://example.com/v.mp4", "cwd": "/outside/dir"},
        ),
    )
    assert not result.allow, "cwd 在投影外时相对输出必须拦截"
