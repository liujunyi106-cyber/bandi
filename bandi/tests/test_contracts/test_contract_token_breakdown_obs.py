# 功能：TokenCounter.breakdown 生产观测契约——LLM 调用路径记录 prompt 组成
"""
生产接线：states._do_llm_call（全部 LLM 生产调用的唯一装配层）在每次调用
   记录 ctx.meta["last_prompt_breakdown"]——按 role 分项计量
分项正确：system/user/assistant/tool 各键值 == 该 role 消息逐条 count 之和
总量一致：分项之和 == 逐条 count 总量（与 _ROLE_KEYS 语义对齐）
dict 消息适配：provider_messages 是 list[dict]（生产形态），breakdown 必须
   直接可消费，不得要求 Message 对象
prompt 组成无法观测，成本/采样分析缺分项数据源。
"""

from typing import Any

import pytest

from bandi.agent.base import AgentConfig, ExecutionContext
from bandi.core.message import LLMResponse, Message, MessageRole


class _StaticProvider:
    protocol = "openai"
    model_name = "deepseek-chat"

    async def complete(self, messages: list, tools: Any = None, **kw: Any) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=[], finish_reason="stop")


class _Bus:
    async def emit(self, *args: Any, **kw: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_llm_call_records_prompt_breakdown(monkeypatch: Any) -> None:
    """+_do_llm_call 生产链路必须把 prompt 分项计量写入 ctx.meta。"""
    from bandi.agent.states import _do_llm_call

    ctx = ExecutionContext(
        messages=[Message(role=MessageRole.USER, content="hi")],
        provider=_StaticProvider(),
        config=AgentConfig(),
        bus=_Bus(),
    )
    ctx.active_tools = []

    msgs = [
        {"role": "system", "content": "你是 Bandi 助手"},
        {"role": "user", "content": "帮我排查 seed 报错"},
        {"role": "assistant", "content": "好的，我来看看"},
        {"role": "tool", "content": "命令执行结果"},
    ]
    result = await _do_llm_call(
        ctx, msgs, use_fallback=False, registry=object(), bandi_config=object()
    )
    assert result is not None

    breakdown = ctx.meta.get("last_prompt_breakdown")
    assert isinstance(breakdown, dict), "生产调用必须记录 last_prompt_breakdown"
    assert set(breakdown) == {"system", "user", "assistant", "tool"}, (
        f"必须按 role 分项计量，实际 {breakdown}"
    )
    assert all(v >= 0 for v in breakdown.values())


@pytest.mark.asyncio
async def test_breakdown_items_sum_to_total() -> None:
    """+分项之和与逐条 count 总量一致；dict 消息直接可消费。"""
    from bandi.utils.token_counter import TokenCounter

    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "用户问题中文内容"},
        {"role": "user", "content": "第二个用户消息"},
        {"role": "assistant", "content": "助手回复"},
        {"role": "tool", "content": "工具结果"},
    ]
    breakdown = TokenCounter.breakdown(msgs)
    assert sum(breakdown.values()) == sum(TokenCounter.count(m["content"]) for m in msgs)
    assert breakdown["user"] == sum(
        TokenCounter.count(m["content"]) for m in msgs if m["role"] == "user"
    )
