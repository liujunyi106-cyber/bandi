"""压缩摘要内容级契约。

1. ``MemoryCompactor.compact``（单块 LLM 摘要，COMPRESSING progressive 最终经过）
2. ``ProgressiveCompressor.compress`` 的 final merge（多块合并路径）

- 发给 LLM 的请求必须携带五节结构指令与保真规则（请求形态）
- LLM 返回文本原样透传进产物，``[上下文摘要]`` 前缀协议稳定（最终产物）
- 不钉真实 LLM 输出必然完美分节（不可控项不入契约）
"""

import pytest

from bandi.agent.states import on_compressing
from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compaction import MemoryCompactor
from bandi.memory.compression_pipeline import (
    COMPACT_BOUNDARY_PREFIX,
    CompressionPipeline,
    ProgressiveCompressor,
    strip_compact_boundaries,
)

_SECTION_MARKERS = (
    "## 目标",
    "## 所有用户消息",
    "## 进度",
    "### Done",
    "### In Progress",
    "### Blocked",
    "## 当前工作",
    "## 关键决策",
    "## 下一步",
    "## 约束与偏好",
    "## 关键上下文",
)
_FAITHFULNESS_MARKERS = ("文件路径", "错误")
_WORD_LIMIT_PHRASE = "300 字以内"

_STRUCTURED_SUMMARY = (
    "## 目标\n- 完成 day124x-a 压缩摘要改造\n\n"
    "## 进度\n### Done\n- [x] 契约测试编写\n"
    "### In Progress\n- [ ] 实现改造\n"
    "### Blocked\n- (none)\n\n"
    "## 关键决策\n- **采用5节模板**: \n\n"
    "## 下一步\n1. 改造 memory/compaction.py\n\n"
    "## 关键上下文\n- 目标模块 bandi/bandi/memory/compaction.py:54 存在单句 prompt\n"
)


class _SummaryCapture:
    """功能：捕获发给 LLM 的摘要请求，并回放预设的结构化摘要响应。"""

    def __init__(self, response_text: str):
        self.requests: list[list[dict]] = []
        self._response_text = response_text

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.requests.append(messages)
        return LLMResponse(
            content=self._response_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


def _long_chinese_messages(count: int) -> list[Message]:
    """功能：构造 system + 中文 + 文件路径 + 错误串混合的真实形态对话消息。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(
            role=MessageRole.USER,
            content=(
                "请修复压缩模块的回归问题，日志见 logs/run_0.py 第 3 行，"
                "错误串 ValueError('compaction failed')"
            ),
        ),
    ]
    for i in range(count):
        msgs.append(
            Message(
                role=MessageRole.USER,
                content=(
                    f"第{i}轮分析：bandi/tests/test_memory 报 AssertionError，"
                    f"日志见 logs/run_{i + 1}.py 第{i}行，错误串 ValueError('compaction failed')"
                ),
            )
        )
        msgs.append(
            Message(role=MessageRole.ASSISTANT, content=f"已定位第{i}个问题在 memory/compaction.py")
        )
    return msgs


async def _run_compact_path(capture: _SummaryCapture) -> list[Message]:
    compactor = MemoryCompactor(target_token_budget=10)
    result, success, _ = await compactor.compact(_long_chinese_messages(3), capture)
    assert success is True, "前置：compact 必须真实触发 LLM 摘要"
    return result


async def _run_progressive_merge_path(capture: _SummaryCapture) -> list[Message]:
    compressor = ProgressiveCompressor(max_chunks=2, target_token_budget=10)
    # 精确 12 条 non_system：max_chunks=2 切成 [6,6] 两块，每块均满足 compact 触发条件
    msgs = [Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手")]
    for i in range(6):
        msgs.append(
            Message(
                role=MessageRole.USER,
                content=f"第{i}轮分析：日志见 logs/run_{i}.py，错误串 ValueError('compaction failed')",
            )
        )
        msgs.append(
            Message(role=MessageRole.ASSISTANT, content=f"已定位第{i}个问题在 memory/compaction.py")
        )
    result, stats = await compressor.compress(msgs, capture)
    assert stats.progressive_failures == 0, "前置：progressive 分块必须全部成功"
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "_runner",
    [_run_compact_path, _run_progressive_merge_path],
    ids=["MemoryCompactor.compact", "ProgressiveCompressor.final_merge"],
)
async def test_every_summary_request_carries_section_structure(_runner) -> None:
    """契约：两条生产路径发出的每个摘要请求都必须携带五节结构指令与保真规则。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _runner(capture)

    assert len(capture.requests) >= 1, "必须至少发出一次摘要请求"
    for request in capture.requests:
        prompt_text = "\n".join(str(m.get("content", "")) for m in request)
        for marker in _SECTION_MARKERS:
            assert marker in prompt_text, f"摘要请求缺少分节指令 {marker!r}（退化风险）"
        for keyword in _FAITHFULNESS_MARKERS:
            assert keyword in prompt_text, f"摘要请求缺少保真规则 {keyword!r}"


@pytest.mark.asyncio
async def test_compact_request_carries_source_text_verbatim() -> None:
    """契约：中文源文本连同文件路径/错误串必须进入摘要请求（不得丢失或预改写）。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _run_compact_path(capture)

    user_texts = [
        str(m.get("content", ""))
        for request in capture.requests
        for m in request
        if m.get("role") == "user"
    ]
    joined = "\n".join(user_texts)
    assert "logs/run_0.py" in joined, "源文本中的文件路径必须进入摘要请求"
    assert "ValueError('compaction failed')" in joined, "源文本中的错误串必须进入摘要请求"


@pytest.mark.asyncio
async def test_compact_product_keeps_summary_message_and_structure() -> None:
    """契约：压缩产物含 ``[上下文摘要]`` SYSTEM 消息，位于 system 区之后 keep 区之前
    且 LLM 返回的分节文本原样透传不被二次加工。"""
    result = await _run_compact_path(_SummaryCapture(_STRUCTURED_SUMMARY))

    summary_indices = [
        i
        for i, m in enumerate(result)
        if m.role == MessageRole.SYSTEM and "[上下文摘要]" in (m.content or "")
    ]
    assert len(summary_indices) == 1, "产物必须恰好包含一条摘要消息"
    idx = summary_indices[0]
    assert result[0].role == MessageRole.SYSTEM and "[上下文摘要]" not in (
        result[0].content or ""
    ), "摘要必须位于原 system 消息之后"
    assert "## 关键决策" in (result[idx].content or ""), "LLM 返回的分节文本必须原样保留"
    assert any(m.role != MessageRole.SYSTEM and m is not result[idx] for m in result[idx + 1 :]), (
        "摘要之后必须保留未压缩的近期消息"
    )


@pytest.mark.asyncio
async def test_progressive_merge_request_receives_chunk_summaries() -> None:
    """契约（破坏者视角：前缀协议断裂检测）：``[上下文摘要]`` 前缀若被改名
    Progressive 将无法从分块产物提取摘要文本送入 merge 请求——本用例钉住该提取链路。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _run_progressive_merge_path(capture)

    assert len(capture.requests) >= 3, "两次分块摘要 + 一次合并摘要至少三次请求"
    merge_requests = [
        r for r in capture.requests if any("请合并以下摘要" in str(m.get("content", "")) for m in r)
    ]
    assert merge_requests, "必须发出 final merge 请求"
    merge_text = "\n".join(str(m.get("content", "")) for m in merge_requests[0])
    assert _STRUCTURED_SUMMARY.split("\n")[0] in merge_text, (
        "分块产出的摘要文本必须经 [上下文摘要] 协议提取并进入合并请求（前缀断裂则此断言红）"
    )


@pytest.mark.asyncio
async def test_summary_prompt_has_no_word_limit_constraint() -> None:
    """契约（决策）：摘要请求不得携带字数硬约束（分节结构下偏紧），由 max_summary_chars 兜底。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _run_compact_path(capture)

    for request in capture.requests:
        prompt_text = "\n".join(str(m.get("content", "")) for m in request)
        assert _WORD_LIMIT_PHRASE not in prompt_text, "摘要请求不得再携带 300 字硬约束"


@pytest.mark.asyncio
async def test_overlong_llm_output_truncated_to_max_summary_chars() -> None:
    """契约（兜底面）：LLM 返回超长时产物摘要仍被 max_summary_chars 截断。"""
    capture = _SummaryCapture("很长的摘要" * 3000)
    result = await _run_compact_path(capture)

    summary_msg = next(m for m in result if "[上下文摘要]" in (m.content or ""))
    body = (summary_msg.content or "").removeprefix("[上下文摘要] ")
    assert len(body) <= 2000, "超长摘要必须被 max_summary_chars 截断兜底"


_STALE_SUMMARY = "STALE-MARKER-旧摘要：第一轮任务的目标与进度记录"
_FRESH_MARK = "FRESH-MARK"


def _messages_with_previous(count: int, previous_text: str) -> list[Message]:
    """功能：构造「已含旧压缩摘要 + 大量新消息」的输入（模拟第二轮压缩场景）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.SYSTEM, content=f"[上下文摘要] {previous_text}"),
    ]
    for i in range(count):
        msgs.append(
            Message(
                role=MessageRole.USER,
                content=f"新一轮第{i}步：修改 memory/compaction.py，错误串 ValueError('incremental')",
            )
        )
        msgs.append(
            Message(role=MessageRole.ASSISTANT, content=f"第{i}步已完成，见 logs/inc_{i}.py")
        )
    return msgs


async def _run_second_compaction(capture: _SummaryCapture) -> list[Message]:
    compactor = MemoryCompactor(target_token_budget=10)
    result, success, _ = await compactor.compact(
        _messages_with_previous(3, _STALE_SUMMARY), capture
    )
    assert success is True, "前置：二次压缩必须真实触发 LLM 摘要"
    return result


@pytest.mark.asyncio
async def test_second_compaction_yields_single_summary() -> None:
    """契约（破坏者视角：双摘要累积回归检测）：含旧摘要的输入再压缩后
    产物必须恰一条 [上下文摘要]，旧摘要消息不得与新摘要并存。"""
    result = await _run_second_compaction(_SummaryCapture(_STRUCTURED_SUMMARY))

    summary_msgs = [
        m for m in result if m.role == MessageRole.SYSTEM and "[上下文摘要]" in (m.content or "")
    ]
    assert len(summary_msgs) == 1, (
        f"二次压缩后必须恰一条摘要消息（发现 {len(summary_msgs)} 条——双摘要累积）"
    )


@pytest.mark.asyncio
async def test_incremental_request_carries_previous_summary_tag() -> None:
    """契约：增量请求必须以 <previous-summary> 标签携带旧摘要原文 + 新消息源文本。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _run_second_compaction(capture)

    user_texts = [
        str(m.get("content", ""))
        for request in capture.requests
        for m in request
        if m.get("role") == "user"
    ]
    joined = "\n".join(user_texts)
    assert "<previous-summary>" in joined and "</previous-summary>" in joined, (
        "增量请求必须包含 <previous-summary> 标签"
    )
    assert _STALE_SUMMARY in joined, "旧摘要文本必须 verbatim 进入 <previous-summary>"
    assert "ValueError('incremental')" in joined, "新增对话内容必须进入请求"


@pytest.mark.asyncio
async def test_incremental_prompt_carries_update_rules() -> None:
    """契约：存在旧摘要时 prompt 必须携带增量更新规则（保留/合并/进度迁移措辞）。
    B1b 1.5 修复后真实 system 作请求前缀（缓存命中），指令后置于尾部 user。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _run_second_compaction(capture)

    tail = str(capture.requests[0][-1].get("content", ""))
    assert "增量" in tail, "增量更新指令缺失"
    for keyword in ("保留", "合并", "In Progress"):
        assert keyword in tail, f"增量规则缺少 {keyword!r}"


@pytest.mark.asyncio
async def test_progressive_merge_excludes_stale_and_includes_fresh() -> None:
    """契约（破坏者视角：提取污染修复）：含旧摘要的多块输入再走 progressive 时
    merge 请求必须收到新块摘要而非把旧摘要误当块摘要（旧摘要只以标签角色出现）。"""
    compressor = ProgressiveCompressor(max_chunks=2, target_token_budget=10)
    fresh_response = _STRUCTURED_SUMMARY.replace("## 目标\n-", f"## 目标\n{_FRESH_MARK}\n-")
    capture = _SummaryCapture(fresh_response)
    msgs = _messages_with_previous(6, _STALE_SUMMARY)
    result, stats = await compressor.compress(msgs, capture)
    assert stats.progressive_failures == 0, "前置：分块全部成功"

    assert len(capture.requests) >= 3, "两次分块 + 一次合并至少三次请求"
    merge_requests = [
        r for r in capture.requests if any("请合并以下摘要" in str(m.get("content", "")) for m in r)
    ]
    chunk_requests = [
        r
        for r in capture.requests
        if not any("请合并以下摘要" in str(m.get("content", "")) for m in r)
    ]
    assert merge_requests and chunk_requests, "前置：块摘要与合并请求均须存在"
    merge_text = "\n".join(str(m.get("content", "")) for m in merge_requests[0])
    assert _FRESH_MARK in merge_text, "merge 必须收到新块摘要"
    chunk_prompts = "\n".join(
        str(m.get("content", "")) for request in chunk_requests for m in request
    )
    assert _STALE_SUMMARY not in chunk_prompts, "分块摘要请求不得携带旧摘要（提取污染）"


@pytest.mark.asyncio
async def test_first_compaction_has_no_previous_summary_tag() -> None:
    """契约（首压回归）：无历史摘要时请求不得出现 <previous-summary> 标签。"""
    capture = _SummaryCapture(_STRUCTURED_SUMMARY)
    await _run_compact_path(capture)

    for request in capture.requests:
        prompt_text = "\n".join(str(m.get("content", "")) for m in request)
        assert "<previous-summary>" not in prompt_text, "首压不得携带增量标签"


class _EmptyMergeCapture:
    """
    分块摘要正常返回，final merge 阶段返回空 content。"""

    def __init__(self) -> None:
        self.requests: list[list[dict]] = []
        self.empty_merge_served = False

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.requests.append(messages)
        is_merge = any("请合并以下摘要" in str(m.get("content", "")) for m in messages)
        if is_merge:
            self.empty_merge_served = True
        return LLMResponse(
            content="" if is_merge else _STRUCTURED_SUMMARY,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_progressive_merge_empty_content_falls_back_to_first_chunk() -> None:
    """
    首块摘要原文——空摘要不得被当作成功产物（免费池波动缺陷回归钉）。"""
    capture = _EmptyMergeCapture()
    result = await _run_progressive_merge_path(capture)

    assert capture.empty_merge_served, (
        "前置失效：merge 空 content 场景未被触发（请求识别词与生产措辞断裂），用例需修正"
    )
    summary_msgs = [
        m for m in result if m.role == MessageRole.SYSTEM and "[上下文摘要]" in (m.content or "")
    ]
    assert len(summary_msgs) == 1, "前置：产物必须恰一条摘要消息"
    body = (summary_msgs[0].content or "").removeprefix("[上下文摘要] ")
    assert _STRUCTURED_SUMMARY.strip() in body, (
        f"merge 空 content 必须回退首块摘要原文（允许确定性附录），实际产物：{body[:80]!r}"
    )


"""行为族（DoD-10）：两条生产调用路径全覆盖——
  1. 自动链 ``states.on_compressing``（Agent Loop COMPRESSING 处理器）
  2. 手动链 ``cli.chat.ChatLoop._compact_session``（REPL /compact）
两者均经 ``CompressionPipeline.compress`` 获得 CompactBoundary 标记，
自动链额外获得 replay/synthetic Continue 任务连续性锚点。
"""

_CONTINUITY_RAW_INPUT = "请修复 logs/boundary.py 第 7 行的错误串 RuntimeError('boundary')"


class _RecordingBus:
    """功能：契约测试用事件总线，只记录不外发。"""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


def _make_continuity_ctx(
    messages: list[Message],
    raw_input: str | None,
    provider: object,
    total_tokens: int = 190,
) -> object:
    """功能：构造可直调 on_compressing 的最小 ExecutionContext（生产形态
    预置跨轮复用 pipeline——生产字段 ctx.compress.pipeline）。"""
    from bandi.agent.base import CompressionState, ExecutionContext
    from bandi.core.config import AgentConfig
    from bandi.memory.compression_pipeline import ProgressiveCompressor

    config = AgentConfig(context_window=200, compression_threshold=0.5)
    ctx = ExecutionContext(
        messages=list(messages),
        provider=provider,
        config=config,
        bus=_RecordingBus(),  # type: ignore[arg-type]
    )
    ctx.raw_input = raw_input
    ctx.total_tokens = total_tokens
    ctx.compress = CompressionState(
        pipeline=CompressionPipeline(
            progressive=ProgressiveCompressor(max_chunks=2, target_token_budget=10)
        )
    )
    return ctx


def _continuity_messages(count: int) -> list[Message]:
    """功能：构造「raw_input 在最前 + 大量后续消息」的对话（模拟长任务中途压缩）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content=_CONTINUITY_RAW_INPUT),
    ]
    for i in range(count):
        msgs.append(
            Message(role=MessageRole.USER, content=f"第{i}步执行日志：logs/step_{i}.py 校验通过")
        )
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}步完成，进入下一步"))
    return msgs


class _ContinuityCapture:
    """功能：捕获摘要请求并回放结构化摘要（同 _SummaryCapture，独立计数便于断言）。"""

    def __init__(self, response_text: str):
        self.requests: list[list[dict]] = []
        self._response_text = response_text

    async def __call__(self, messages: list[dict]) -> object:
        from bandi.core.message import LLMResponse, TokenUsage

        self.requests.append(messages)
        return LLMResponse(
            content=self._response_text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


class _StubCountingProvider:
    """功能：最小 provider 替身——complete 委托捕获器，count_tokens 按 4 字符/Token 估算。"""

    def __init__(self, complete) -> None:
        self._complete = complete

    async def complete(self, messages, tools=None, **kwargs):
        return await self._complete(messages)

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)


def _boundary_msgs(messages: list[Message]) -> list[Message]:
    return [
        m
        for m in messages
        if m.role == MessageRole.SYSTEM and (m.content or "").startswith(COMPACT_BOUNDARY_PREFIX)
    ]


def _summary_msgs(messages: list[Message]) -> list[Message]:
    return [
        m for m in messages if m.role == MessageRole.SYSTEM and "[上下文摘要]" in (m.content or "")
    ]


async def _run_pipeline_once(messages: list[Message], capture: _ContinuityCapture) -> tuple:
    """功能：走真实 CompressionPipeline 单次压缩（生产参数：低窗口强制触发）。"""
    from bandi.memory.compression_pipeline import ProgressiveCompressor

    pipeline = CompressionPipeline(
        progressive=ProgressiveCompressor(max_chunks=2, target_token_budget=10)
    )
    result, success, _, stats = await pipeline.compress(messages, 190, 200, 0.5, capture)
    assert success is True and stats.stage != "none", "前置：必须发生真实压缩"
    return result, stats


@pytest.mark.asyncio
async def test_pipeline_inserts_single_boundary_marker() -> None:
    """契约：真实压缩发生后产物必须恰含一条 [压缩边界] SYSTEM 标记消息。"""
    capture = _ContinuityCapture(_STRUCTURED_SUMMARY)
    result, _ = await _run_pipeline_once(_continuity_messages(8), capture)

    boundaries = _boundary_msgs(result)
    assert len(boundaries) == 1, f"压缩产物必须恰一条 [压缩边界] 消息（发现 {len(boundaries)} 条）"


@pytest.mark.asyncio
async def test_boundary_positioned_before_summary_and_dialogue() -> None:
    """
    boundaryMarker → summaryMessages → kept 顺序）。"""
    capture = _ContinuityCapture(_STRUCTURED_SUMMARY)
    result, _ = await _run_pipeline_once(_continuity_messages(8), capture)

    boundary_idx = next(i for i, m in enumerate(result) if m in _boundary_msgs(result))
    summary_idx = next((i for i, m in enumerate(result) if m in _summary_msgs(result)), None)
    assert summary_idx is not None, "前置：产物应含摘要"
    assert boundary_idx < summary_idx, "boundary 必须在摘要之前"
    non_system_after = [m for m in result[boundary_idx + 1 :] if m.role != MessageRole.SYSTEM]
    assert non_system_after, "前置：boundary 之后应有保留对话"
    non_system_before = [m for m in result[:boundary_idx] if m.role != MessageRole.SYSTEM]
    assert not non_system_before, "boundary 之前不得有对话消息"


@pytest.mark.asyncio
async def test_second_compression_yields_single_boundary_and_summary() -> None:
    """契约（破坏者视角：标记累积回归检测）：含旧 boundary + 旧摘要的输入再压缩后，"""
    first_capture = _ContinuityCapture(_STRUCTURED_SUMMARY)
    first_result, _ = await _run_pipeline_once(_continuity_messages(8), first_capture)

    tail = [
        Message(role=MessageRole.USER, content=f"续作第{j}步：logs/more_{j}.py")
        for j in range(8)
        for _ in (Message(role=MessageRole.ASSISTANT, content=f"第{j}步完成"),)
    ]
    second_input = first_result + tail
    second_capture = _ContinuityCapture(_STRUCTURED_SUMMARY)
    second_result, _ = await _run_pipeline_once(second_input, second_capture)

    assert len(_boundary_msgs(second_result)) == 1, "二次压缩后 boundary 必须恒一条"
    assert len(_summary_msgs(second_result)) == 1, "二次压缩后摘要必须恒一条（b 日回归）"


@pytest.mark.asyncio
async def test_boundary_content_carries_stage_and_message_counts() -> None:
    """契约：boundary 内容必须携带 stage 与前后消息数（压缩事件可观测性）。"""
    capture = _ContinuityCapture(_STRUCTURED_SUMMARY)
    result, stats = await _run_pipeline_once(_continuity_messages(8), capture)

    content = _boundary_msgs(result)[0].content or ""
    assert f"stage={stats.stage}" in content, "boundary 必须记录压缩 stage"
    assert "msgs" in content and "→" in content, "boundary 必须记录前后消息数"


@pytest.mark.asyncio
async def test_boundary_strip_does_not_touch_regular_system_messages() -> None:
    """契约（破坏者视角：前缀协议防误伤）：strip 只剥 [压缩边界] 前缀消息
    普通 system 消息（含其他方括号标记）不得被误删；非标准前缀视为协议断裂不被识别。"""
    msgs = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.SYSTEM, content="[系统提示] 这不是压缩边界"),
        Message(role=MessageRole.SYSTEM, content="[压缩边界] turn=1 stage=micro msgs 9→4"),
        Message(role=MessageRole.USER, content="hi"),
    ]
    stripped = strip_compact_boundaries(msgs)

    assert len(stripped) == 3, "只剥标准前缀的 boundary"
    assert any("你是 Bandi" in (m.content or "") for m in stripped), "原 system 保留"
    assert any("[系统提示]" in (m.content or "") for m in stripped), "非标准前缀不误伤"
    assert not any(m in stripped for m in msgs if "[压缩边界]" in (m.content or ""))


@pytest.mark.asyncio
async def test_on_compressing_replays_raw_input_when_pruned() -> None:
    """契约：压缩裁掉本轮 raw_input 时，必须在尾部 re-append 原始输入
    （opencode replay 语义），不得让 LLM 失去任务锚点。"""
    provider_calls: list[list[dict]] = []

    async def capture_complete(messages: list[dict]) -> object:
        provider_calls.append(messages)
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(
            content=_STRUCTURED_SUMMARY,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    provider = _StubCountingProvider(capture_complete)
    ctx = _make_continuity_ctx(_continuity_messages(10), _CONTINUITY_RAW_INPUT, provider)
    await on_compressing(ctx)

    tail_user = [m for m in ctx.messages if m.role == MessageRole.USER][-1]
    assert tail_user.content == _CONTINUITY_RAW_INPUT, (
        "raw_input 被压缩裁掉时必须 re-append 到尾部（replay）"
    )
    assert len(provider_calls) >= 1, "前置：必须发生真实 LLM 摘要"


@pytest.mark.asyncio
async def test_on_compressing_injects_synthetic_continue_without_raw_input() -> None:
    """契约：无 raw_input 时压缩后必须注入 synthetic Continue user 消息
    （opencode compaction_continue 语义），提示 LLM 继续推进或澄清。"""

    async def capture_complete(messages: list[dict]) -> object:
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(
            content=_STRUCTURED_SUMMARY,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    provider = _StubCountingProvider(capture_complete)
    ctx = _make_continuity_ctx(_continuity_messages(10), None, provider)
    await on_compressing(ctx)

    tail_user = [m for m in ctx.messages if m.role == MessageRole.USER]
    assert tail_user, "必须存在注入的续作锚点"
    anchor = tail_user[-1]
    assert anchor.meta.get("synthetic") is True, "续作消息必须带 synthetic 标记"
    assert "继续" in (anchor.content or ""), "续作消息必须携带继续指令"


@pytest.mark.asyncio
async def test_on_compressing_skips_anchor_when_input_retained() -> None:
    """契约：raw_input 仍在保留区时不得注入任何锚点（上下文连续性完好
    注入即冗余 token 与噪音）。"""

    async def capture_complete(messages: list[dict]) -> object:
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(
            content=_STRUCTURED_SUMMARY,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    provider = _StubCountingProvider(capture_complete)
    # raw_input 位于对话尾部（用户刚发言后触发压缩）——任何策略都保留尾部
    msgs: list[Message] = [Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手")]
    for i in range(8):
        msgs.append(
            Message(role=MessageRole.USER, content=f"第{i}步执行日志：logs/step_{i}.py 校验通过")
        )
        msgs.append(Message(role=MessageRole.ASSISTANT, content=f"第{i}步完成，进入下一步"))
    msgs.append(Message(role=MessageRole.USER, content=_CONTINUITY_RAW_INPUT))
    ctx = _make_continuity_ctx(msgs, _CONTINUITY_RAW_INPUT, provider)
    await on_compressing(ctx)

    user_contents = [m.content for m in ctx.messages if m.role == MessageRole.USER]
    assert user_contents.count(_CONTINUITY_RAW_INPUT) == 1, "raw_input 保留时不得重复注入 replay"
    assert all(
        (m.meta or {}).get("synthetic") is not True
        for m in ctx.messages
        if m.role == MessageRole.USER
    ), "不得注入 synthetic Continue"


@pytest.mark.asyncio
async def test_anchor_not_duplicated_within_same_turn() -> None:
    """契约（破坏者视角：双注入检测）：同一 turn 内第二次压缩不得再次注入锚点
    （continue_injected 标志防重）。"""
    calls: list[list[dict]] = []

    async def capture_complete(messages: list[dict]) -> object:
        calls.append(messages)
        from bandi.core.message import LLMResponse, TokenUsage

        return LLMResponse(
            content=_STRUCTURED_SUMMARY,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )

    provider = _StubCountingProvider(capture_complete)
    ctx = _make_continuity_ctx(_continuity_messages(10), None, provider)
    await on_compressing(ctx)

    synthetic_count_1 = sum(
        1 for m in ctx.messages if m.role == MessageRole.USER and m.meta.get("synthetic")
    )
    assert synthetic_count_1 == 1, "前置：首次压缩恰注入一条"

    # 模拟同轮再次触发压缩（新消息再次推高 token）
    ctx.messages.extend(
        [Message(role=MessageRole.ASSISTANT, content=f"更多输出 {list(range(150))}")]
    )
    ctx.total_tokens = sum(ctx.provider.count_tokens(m.content or "") for m in ctx.messages)
    await on_compressing(ctx)

    synthetic_count_2 = sum(
        1 for m in ctx.messages if m.role == MessageRole.USER and m.meta.get("synthetic")
    )
    assert synthetic_count_2 <= 1, (
        f"同轮二次压缩后 synthetic 锚点不得累积（当前 {synthetic_count_2}）"
    )
