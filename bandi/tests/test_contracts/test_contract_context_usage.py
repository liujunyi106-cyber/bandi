# 功能：契约——上下文用量五类分桶（Composer 用量环与面板的数据口径）。
# 钉行为：统计对象是真正发出的请求（system / 工具面 / 技能工件 / 对话消息 / MCP schema）
# 各自归因正确、总量等于分项之和、端点按窗口算比例、无快照不伪造数字、未知会话 404。
from __future__ import annotations

import sys
from typing import Any

import pytest

from bandi.agent.context_usage import BUCKETS, build_snapshot


def _fresh_store() -> Any:
    """功能：取隔离后的 API 单例 store（先清进程级缓存，防串用他用例临时目录）。"""
    from bandi.api.app import _get_store

    api_module = sys.modules["bandi.api.app"]
    with api_module._store_lock:
        api_module._store = None
    return _get_store()


@pytest.fixture()
def api_key(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """功能：注入固定 API key——中间件按模块全局实时读取，测试用同一值发 Bearer。"""
    module = sys.modules["bandi.api.app"]
    monkeypatch.setattr(module, "_API_KEY", "test-key", raising=False)
    return {"Authorization": "Bearer test-key"}


def test_snapshot_attributes_five_buckets() -> None:
    snapshot = build_snapshot(
        messages=[
            {"role": "system", "content": "你是 Bandi 的员工助手。" * 20},
            {"role": "system", "content": "## Runtime Artifact: 技能\n- 技能索引正文"},
            {"role": "system", "content": "## Runtime Artifact Delta: 可用工具\n- read"},
            {"role": "user", "content": "帮我看下这个文件"},
            {"role": "tool", "content": "文件内容若干"},
        ],
        tools=[
            {"name": "read", "description": "读文件"},
            {"name": "srv__search", "description": "远端检索"},
        ],
        mcp_tool_names={"srv__search"},
        model="deepseek-v4.1-flash",
        context_window=1_000_000,
    )

    counts = snapshot["buckets"]
    assert set(counts) == set(BUCKETS)
    for bucket in BUCKETS:
        assert counts[bucket] > 0, f"{bucket} 未归因到任何 token"
    assert snapshot["used_tokens"] == sum(counts.values())
    assert snapshot["context_window"] == 1_000_000

    # 破坏者：MCP 名单为空时同一条 schema 计入 tools（不得静默丢弃）
    plain = build_snapshot(messages=[], tools=[{"name": "srv__search", "description": "x"}])
    assert plain["buckets"]["connectors"] == 0
    assert plain["buckets"]["tools"] > 0


def test_snapshot_counts_tool_arguments_and_wrapped_mcp_schema() -> None:
    """tool_calls 参数与 OpenAI 包装 schema 必须按真实请求口径归因。"""
    snapshot = build_snapshot(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": '{"content":"' + "x" * 600 + '}"',
                        },
                    }
                ],
            }
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": "srv__search", "description": "远端检索"},
            }
        ],
        mcp_tool_names={"srv__search"},
    )
    assert snapshot["buckets"]["messages"] > 50, "tool_calls 参数未计入对话消息"
    assert snapshot["buckets"]["connectors"] > 0, "OpenAI 包装 schema 未识别为 MCP"


def test_snapshot_counts_tool_schema_separately_from_messages() -> None:
    # 破坏者：只有工具 schema、没有对话 → messages 必须为 0（防把 schema 算进对话）
    snapshot = build_snapshot(
        messages=[{"role": "system", "content": "系统"}],
        tools=[{"name": "bash", "description": "执行命令", "parameters": {"type": "object"}}],
    )
    assert snapshot["buckets"]["messages"] == 0
    assert snapshot["buckets"]["tools"] > 0


def test_endpoint_returns_buckets_with_ratio(api_key: dict[str, str]) -> None:
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    store = _fresh_store()
    session_id = store.create_session(title="用量契约")
    store.save_context_usage(
        session_id,
        {
            "model": "deepseek-v4.1-flash",
            "context_window": 100_000,
            "used_tokens": 25_000,
            "buckets": {
                "system": 10_000,
                "tools": 5_000,
                "messages": 8_000,
                "connectors": 1_000,
                "skills": 1_000,
            },
            "updated_at": "2026-10-25T07:00:00",
        },
    )

    with TestClient(app) as client:
        body = client.get(f"/api/sessions/{session_id}/context-usage", headers=api_key).json()

    assert body["available"] is True
    assert body["model"] == "deepseek-v4.1-flash"
    assert body["used_tokens"] == 25_000
    assert body["ratio"] == 0.25
    assert [item["key"] for item in body["buckets"]] == list(BUCKETS)
    assert body["buckets"][0]["tokens"] == 10_000
    assert body["buckets"][0]["ratio"] == 0.1


def test_endpoint_without_snapshot_reports_unavailable(api_key: dict[str, str]) -> None:
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    store = _fresh_store()
    session_id = store.create_session(title="无快照")

    with TestClient(app) as client:
        body = client.get(f"/api/sessions/{session_id}/context-usage", headers=api_key).json()

    assert body["available"] is False
    assert body["used_tokens"] == 0
    assert body["context_window"] == 0
    assert body["ratio"] == 0.0
    assert all(item["tokens"] == 0 and item["ratio"] == 0.0 for item in body["buckets"])


def test_endpoint_unknown_session_is_404(api_key: dict[str, str]) -> None:
    from fastapi.testclient import TestClient

    from bandi.api.app import app

    _fresh_store()
    with TestClient(app) as client:
        response = client.get("/api/sessions/no-such-session/context-usage", headers=api_key)

    assert response.status_code == 404
    # 统一错误体由中间件包装（code/message），因此按原文匹配而非取 detail 键
    assert "SESSION_NOT_FOUND" in response.text


def test_wiring_request_assembly_records_snapshot() -> None:
    """接线：真实请求组装入口落库——跑一次 _build_provider_messages 即可读到快照。"""
    from bandi.agent.base import ExecutionContext
    from bandi.agent.states import _build_provider_messages
    from bandi.core.config import AgentConfig
    from bandi.core.event_bus import EventBus
    from bandi.core.message import Message, MessageRole
    from bandi.llm.mock import MockProvider

    store = _fresh_store()
    session_id = store.create_session(title="接线")
    ctx = ExecutionContext(
        messages=[
            Message(role=MessageRole.SYSTEM, content="你是 Bandi 的员工助手。" * 30),
            Message(role=MessageRole.USER, content="这轮上下文用了多少"),
        ],
        provider=MockProvider(),
        config=AgentConfig(context_window=1_000_000),
        bus=EventBus(),
    )
    ctx.session_id = session_id
    ctx.meta["session_store"] = store
    ctx.active_tools = [{"name": "read", "description": "读文件"}]

    _build_provider_messages(ctx)

    snapshot = store.get_context_usage(session_id)
    assert snapshot is not None, "请求组装必须落一份快照（面板数据源）"
    assert snapshot["context_window"] == 1_000_000
    assert snapshot["buckets"]["system"] > 0
    assert snapshot["buckets"]["messages"] > 0
    assert snapshot["buckets"]["tools"] > 0
    assert snapshot["used_tokens"] == sum(snapshot["buckets"].values())

    # 破坏者：同一请求重复组装（流式失败转非流式）不得重复累计
    first_total = snapshot["used_tokens"]
    _build_provider_messages(ctx)
    assert store.get_context_usage(session_id)["used_tokens"] == first_total
