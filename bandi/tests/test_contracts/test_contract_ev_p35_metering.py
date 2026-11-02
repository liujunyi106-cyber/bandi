"""契约族：EV-计量贯通 + T-smoke-④ refusal 文案统一。

生产调用路径：
1. `agent/states.py::memory_section_tokens`——记忆注入段落 token 计量
   （memory_tokens 此前恒 0，成本拆解不可观测）
2. `agent/states.py::_do_llm_call_inner`——每次真实 LLM 调用自增 attempt 计数
   （attempt_id 此前恒 0，无法区分「一次成」与「重试成」）
3. `agent/states.py::on_parsing`——SAFETY_BLOCKED 固定文案为终态唯一来源
   不得与 current_content 残留模型原话竞争（T-smoke-④）
4. `agent/verifier.py::VerifierPolicy`——max_revisions 死字段已删（残余）
   返工上限的真实权威是 stagnation_threshold
"""

from __future__ import annotations

import asyncio

from bandi.agent.base import ExecutionContext, TerminalReason
from bandi.core.config import AgentConfig
from bandi.core.event_bus import EventBus
from bandi.core.message import LLMResponse


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        messages=[],
        provider=object(),
        config=AgentConfig(),
        bus=EventBus(),
    )


# ---------- memory_tokens ----------


def test_memory_section_tokens_counts_context_section() -> None:
    from bandi.agent.states import memory_section_tokens

    messages = [
        {"role": "system", "content": "你是 Bandi 助手，请遵守工具协议。"},
        {
            "role": "system",
            "content": "## 上下文参考\n\n[long_term] 飞书渠道配置步骤：先建应用，再配 webhook。",
        },
    ]
    assert memory_section_tokens(messages) > 0, "记忆段落必须被计量（此前恒 0）"


def test_memory_section_tokens_counts_prefetch_block() -> None:
    from bandi.agent.states import memory_section_tokens

    messages = [
        {"role": "system", "content": "## Runtime Prefetch\n\n## 相关记忆\n用户偏好中文回复。"},
    ]
    assert memory_section_tokens(messages) > 0, "预取记忆块必须被计量"


def test_memory_section_tokens_zero_without_memory() -> None:
    from bandi.agent.states import memory_section_tokens

    messages = [
        {"role": "system", "content": "系统提示词，无记忆段落。"},
        {"role": "user", "content": "## 上下文参考 出现在用户消息里不算记忆注入"},
    ]
    assert memory_section_tokens(messages) == 0, "无记忆注入时不得虚报"


def test_memory_section_tokens_stops_at_next_section() -> None:
    from bandi.agent.states import memory_section_tokens

    only_memory = memory_section_tokens([{"role": "system", "content": "## 上下文参考\n短记忆。"}])
    with_next_section = memory_section_tokens(
        [{"role": "system", "content": "## 上下文参考\n短记忆。\n## 公司上下文\n这是公司段落。"}]
    )
    assert only_memory == with_next_section, "段落边界必须止于下一个二级标题"


# ---------- attempt_id ----------


class _StubProvider:
    model_name = "stub"
    protocol = "openai"
    supports_streaming = False

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[dict], **kwargs: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="ok", finish_reason="stop")


def test_llm_attempt_counter_increments_per_call() -> None:
    from bandi.agent.states import _do_llm_call_inner

    ctx = _ctx()
    ctx.provider = _StubProvider()
    ctx.meta["llm_attempt"] = 0
    messages = [{"role": "user", "content": "hi"}]

    asyncio.run(_do_llm_call_inner(ctx, messages, False, None, None, allow_stream=False))
    assert ctx.meta["llm_attempt"] == 1, "首次 LLM 调用须计 1"

    asyncio.run(_do_llm_call_inner(ctx, messages, False, None, None, allow_stream=False))
    assert ctx.meta["llm_attempt"] == 2, "重试/二次调用须自增（attempt_id 不再恒 0）"


def test_refusal_maps_to_fixed_message_and_clears_content() -> None:
    from bandi.agent.states import on_parsing

    ctx = _ctx()
    ctx.current_response = LLMResponse(
        content="我不能帮你做这件事。", tool_calls=[], finish_reason="stop", refusal=True
    )
    ctx.current_content = "我不能帮你做这件事。"

    asyncio.run(on_parsing(ctx))

    assert ctx.terminal_reason == TerminalReason.SAFETY_BLOCKED
    assert ctx.final_message == "模型拒绝回答", "固定文案是 SAFETY_BLOCKED 的终态唯一来源"
    assert ctx.current_content is None, "不得残留模型原话与固定文案竞争"


def test_content_filter_maps_to_fixed_message_and_clears_content() -> None:
    from bandi.agent.states import on_parsing

    ctx = _ctx()
    ctx.current_response = LLMResponse(
        content="被过滤的片段", tool_calls=[], finish_reason="content_filter"
    )
    ctx.current_content = "被过滤的片段"

    asyncio.run(on_parsing(ctx))

    assert ctx.terminal_reason == TerminalReason.SAFETY_BLOCKED
    assert ctx.final_message == "模型拒绝回答"
    assert ctx.current_content is None


def test_verifier_policy_dropped_dead_max_revisions() -> None:
    from bandi.agent.verifier import VerifierPolicy

    policy = VerifierPolicy()
    assert not hasattr(policy, "max_revisions"), "max_revisions 死字段已删（零读零配置）"
    assert policy.stagnation_threshold == 3, "返工上限真实权威仍是 stagnation_threshold"
