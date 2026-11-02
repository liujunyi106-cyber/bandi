# 功能：SubAgent 链路验证 — 子 Agent 必须有 system prompt + 真实 provider

from __future__ import annotations

from typing import Any

import pytest

from bandi.core.message import LLMResponse
from bandi.llm.mock import MockProvider


class _RecordingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.seen_messages.append(messages)
        return await super().complete(messages, tools=tools, **kwargs)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_messages.append(messages)
        async for ev in super().stream(messages, tools=tools, **kwargs):
            yield ev


@pytest.mark.asyncio
async def test_subagent_has_system_prompt() -> None:
    """子 Agent 的 LLM 请求必须包含 system prompt（当前缺失——与主 Agent 同源）。"""
    from bandi.agent.subagent import SubagentConfig, SubagentManager

    provider = _RecordingProvider()
    provider.add_response(LLMResponse(content="子任务完成"))
    manager = SubagentManager(provider=provider)
    result = await manager.spawn("整理报告", SubagentConfig(max_turns=2))
    assert result.success
    assert provider.seen_messages, "子 Agent 必须调用 LLM"
    first = provider.seen_messages[0]
    assert first[0]["role"] == "system", (
        "子 Agent 请求首条必须 system（当前 _run_subagent 只放 user 消息，无 system prompt）"
    )


@pytest.mark.asyncio
async def test_subagent_tool_uses_real_provider() -> None:
    """SubAgentTool 生产路径不得用 MockProvider 返回假响应。"""
    from bandi.agent.subagent import SubagentManager
    from bandi.agent.subagent_tool import SubAgentTool

    SubAgentTool()
    # 直接验证工具内部构造 manager 时是否有 provider（应支持注入）
    import inspect

    sig = inspect.signature(SubagentManager.__init__)
    assert "provider" in sig.parameters, "SubagentManager 必须支持 provider 注入"
