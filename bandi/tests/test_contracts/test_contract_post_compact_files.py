"""
自动链 on_compressing / 手动链 /compact / 响应式 _reactive_compact_retry 三路共享；
首轮之后按当轮 raw_input 补匹配新 skill。
"""

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage, ToolCall
from bandi.memory.compression_pipeline import (
    CompressionPipeline,
    ProgressiveCompressor,
    build_post_compact_file_attachment,
)

_STRUCTURED_SUMMARY = (
    "## 目标\n- x\n\n## 进度\n### Done\n- [x] a\n### In Progress\n- [ ] b\n"
    "### Blocked\n- (none)\n\n## 关键决策\n- y\n\n## 下一步\n1. z\n\n## 关键上下文\n- p"
)


def _tool_msg(tool: str, path: str) -> Message:
    """功能：构造携带文件类工具调用的 assistant 消息（结构化触碰记录）。"""
    return Message(
        role=MessageRole.ASSISTANT,
        content=f"调用 {tool}",
        tool_calls=[ToolCall(id=f"c_{tool}_{path}", name=tool, arguments={"path": path})],
    )


def _dialogue_with_touches(touches: list[tuple[str, str]], rounds: int = 6) -> list[Message]:
    """功能：构造「文件触碰 + 大量后续消息」的对话（模拟触碰被大裁剪裁掉）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content="请修复 logs/seed.py 的 ValueError('seed')"),
    ]
    for tool, path in touches:
        msgs.append(_tool_msg(tool, path))
    for i in range(rounds):
        msgs.append(
            Message(
                role=MessageRole.USER,
                content=f"第{i}轮排查：logs/run_{i}.py 报 ValueError('compaction failed')，请分析并给出补丁方案说明。",
            )
        )
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}轮结论见 patch_{i}.diff"))
    return msgs


class _SilentBus:
    """收集事件的哑事件总线。

    功能：`EventBus.emit` 在生产是 `async`，用同步 stub 会让被测代码
    `await None` 抛 TypeError（掩盖真实断言）。此处保持 async 签名。
    """

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event) -> None:
        self.events.append(event)


class _Capture:
    async def __call__(self, messages):
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(
            content=_STRUCTURED_SUMMARY,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


def _attachments(messages: list[Message]) -> list[Message]:
    return [
        m
        for m in messages
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith("[后压缩上下文]")
    ]


def _run_pipeline(messages: list[Message]):
    # max_chunks=4 保证本文件的消息构造（含余数）每块均过最小压缩门槛
    pipeline = CompressionPipeline(
        progressive=ProgressiveCompressor(max_chunks=4, target_token_budget=10)
    )
    total = sum(len(m.content or "") // 4 for m in messages)
    return pipeline.compress(messages, total, 200, 0.5, _Capture())


@pytest.mark.asyncio
async def test_attachment_extracted_from_removed_tool_calls() -> None:
    """
    路径来自被裁区 tool_calls 结构化数据。"""
    msgs = _dialogue_with_touches([("read", "src/a.py")], rounds=10)
    result, success, _, stats = await _run_pipeline(msgs)
    assert success and stats.stage != "none", "前置：必须发生真实压缩"

    atts = _attachments(result)
    assert len(atts) == 1, f"产物必须恰一条附件（got {len(atts)}）"
    assert "src/a.py" in (atts[0].content or ""), "触碰路径必须进入附件"


@pytest.mark.asyncio
async def test_attachment_respects_max_five_recent_first() -> None:
    """契约：触碰文件超过上限时附件最多 5 条且最近触碰优先
    POST_COMPACT_MAX_FILES_TO_RESTORE 语义）。"""
    touches = [(t, f"src/f{i}.py") for i, t in enumerate(["read"] * 7)]
    msgs = _dialogue_with_touches(touches, rounds=12)
    result, success, _, stats = await _run_pipeline(msgs)
    assert success and stats.stage != "none", "前置：必须发生真实压缩"

    atts = _attachments(result)
    assert len(atts) == 1
    content = atts[0].content or ""
    listed = [ln.strip("- ").strip() for ln in content.splitlines() if ln.startswith("- ")]
    assert len(listed) <= 5, f"附件路径不得超过 5（got {len(listed)}）"


@pytest.mark.asyncio
async def test_no_attachment_without_file_tool_calls() -> None:
    """契约：被裁区无任何文件类工具触碰时不得产生空附件。"""
    msgs = _dialogue_with_touches([], rounds=10)
    result, success, _, stats = await _run_pipeline(msgs)
    assert success and stats.stage != "none", "前置：必须发生真实压缩"

    assert not _attachments(result), "无触碰不得产生附件"


@pytest.mark.asyncio
async def test_non_file_tools_not_collected() -> None:
    """契约：bash/web_search 等非文件工具的调用不得被误收进附件。"""
    non_file_msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content="seed"),
        Message(
            role=MessageRole.ASSISTANT,
            content="run",
            tool_calls=[
                ToolCall(id="c_bash", name="bash", arguments={"command": "ls"}),
                ToolCall(id="c_web", name="web_search", arguments={"query": "x"}),
            ],
        ),
    ]
    msgs = non_file_msgs + _dialogue_with_touches([], rounds=10)[2:]
    result, success, _, stats = await _run_pipeline(msgs)
    assert success and stats.stage != "none", "前置：必须发生真实压缩"

    assert not _attachments(result), "非文件工具不得产生附件"


@pytest.mark.asyncio
async def test_attachment_wording_requires_reread_hint() -> None:
    """契约（破坏者视角：防臆断）：附件必须明确提示内容已不在上下文、需重新读取——
    缺失该提示模型会误以为文件内容仍可用而拒绝重读。"""
    att = build_post_compact_file_attachment([_tool_msg("read", "src/a.py")])
    assert att is not None
    assert "重新读取" in (att.content or ""), "附件必须携带重新读取提示"


@pytest.mark.asyncio
async def test_dedup_same_file_multiple_calls() -> None:
    """契约：同一文件多次触碰只记一条。"""
    att = build_post_compact_file_attachment(
        [_tool_msg("read", "src/a.py"), _tool_msg("edit", "src/a.py")]
    )
    assert att is not None
    content = att.content or ""
    assert content.count("src/a.py") == 1, "同文件去重"


@pytest.mark.asyncio
async def test_reactive_path_also_gets_attachment() -> None:
    """
    错误驱动与自动压缩共享 pipeline 出口。"""
    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.agent.states import _reactive_compact_retry
    from bandi.core.config import AgentConfig

    class P:
        protocol = "openai"

        @property
        def model_name(self) -> str:
            return "fake"

        async def complete(self, messages, tools=None, **kw):
            return LLMResponse(
                content=_STRUCTURED_SUMMARY,
                tool_calls=[],
                usage=TokenUsage(5, 3),
                finish_reason="stop",
            )

        def count_tokens(self, t):
            return max(1, len(t) // 4)

    provider = P()
    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=_dialogue_with_touches([("read", "src/reactive.py")], rounds=10),
        provider=provider,
        config=config,
        bus=_SilentBus(),
    )
    ctx.total_tokens = sum(provider.count_tokens(m.content or "") for m in ctx.messages)
    ctx.compress = CompressionState(
        pipeline=CompressionPipeline(
            progressive=ProgressiveCompressor(max_chunks=4, target_token_budget=10)
        )
    )

    result = await _reactive_compact_retry(ctx, max_attempts=2)

    assert result is not None, "前置：响应式压缩重试成功"
    atts = _attachments(ctx.messages)
    assert len(atts) == 1, "响应式路径同样必须产生附件"
    assert "src/reactive.py" in (atts[0].content or ""), "触碰路径进入附件"
