"""契约：LLM 摘要产物实体保真校验——源文本中的关键实体（文件路径/错误串/函数
调用/数值）必须被摘要保留；遗漏超阈值触发「重试一次 + degradation 告警」；产物仍
接受（不阻断主链）；保真不足不触碰压缩失败计数（独立维度）。

  1. MemoryCompactor.compact —— compaction.py（单块全量摘要）
  2. ProgressiveCompressor.compress 分块路径 —— compression_pipeline.py
     （内部复用 MemoryCompactor，故单块下与 compact 行为同构）
  3. ProgressiveCompressor.compress final merge —— compression_pipeline.py
契约钉行为不钉实现：只断言「最终摘要内容 + 请求/重试次数 + 告警日志 + 统计」。
"""

from __future__ import annotations

import logging

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compaction import SUMMARY_MESSAGE_PREFIX, MemoryCompactor
from bandi.memory.compression_pipeline import CompressionStats, ProgressiveCompressor
from bandi.memory.fidelity import (
    FIDELITY_RETENTION_THRESHOLD,
    build_fidelity_retry_hint,
    check_fidelity,
    extract_fidelity_entities,
)

# ── 实体集（源与摘要共享，确保「保真通过/遗漏」精确可控）─────────────────────
_REALITY_FILE = "logs/run_0.py"
_REALITY_ERROR = "ValueError('compaction failed')"
_REALITY_CALL = "validate_ctx()"
_REALITY_NUMBER = "8080"
_SOURCE_REALITIES = (
    f"请修复压缩模块回归，日志见 {_REALITY_FILE} 端口 {_REALITY_NUMBER}，"
    f"错误串 {_REALITY_ERROR}，目标函数 {_REALITY_CALL}"
)
_EXPECTED_SOURCE_ENTITIES_KINDS = {"filepath", "error", "call", "number"}


def _full_summary() -> str:
    """功能：保留全部源实体的保真摘要。"""
    return (
        "## 目标\n- 修复压缩模块\n\n"
        "## 进度\n### Done\n- [x] 定位日志\n"
        "### In Progress\n- [ ] 修复\n### Blocked\n- (none)\n\n"
        "## 关键决策\n- **采用5节模板**: 保留实体\n\n"
        "## 下一步\n1. 改造\n\n"
        f"## 关键上下文\n- {_REALITY_FILE} {_REALITY_ERROR} {_REALITY_CALL} 端口 {_REALITY_NUMBER}\n"
    )


def _omitting_summary() -> str:
    """功能：符合五节结构但「遗漏全部实体」的摘要（构造保真缺失最坏情形）。"""
    return (
        "## 目标\n- 修复压缩模块\n\n"
        "## 进度\n### Done\n- [x] 定位日志\n"
        "### In Progress\n- [ ] 修复\n### Blocked\n- (none)\n\n"
        "## 关键决策\n- **采用5节模板**: 保留结构\n\n"
        "## 下一步\n1. 改造\n\n"
        "## 关键上下文\n- (none)\n"
    )


class _SeqCapture:
    """功能：顺序回放预设摘要响应（支持重试：队列耗尽返回空串）。"""

    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.requests: list[list[dict]] = []

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.requests.append(messages)
        text = self._responses.pop(0) if self._responses else ""
        return LLMResponse(
            content=text,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


def _make_msgs(entity_text: str, n: int, turn_suffix: str) -> list[Message]:
    """功能：构造 system + 含实体的对话（供 compact/progressive 共用）。"""
    msgs: list[Message] = [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 数字员工助手"),
        Message(role=MessageRole.USER, content=f"{entity_text} {turn_suffix}"),
    ]
    if n > 1:
        for i in range(n - 1):
            msgs.append(Message(role=MessageRole.USER, content=f"第{i}轮：{entity_text}"))
            msgs.append(Message(role=MessageRole.ASSISTANT, content=f"已处理 {i}"))
    return msgs


def _single_turn_msgs() -> list[Message]:
    """功能：compact 源——实体集中于首个 user（作 compress 目标），后接无实体轮
    次撑起 non_system≥3 以触发压缩（分割点落在首条之后）。"""
    return _make_msgs(_SOURCE_REALITIES, 1, "请分析") + [
        Message(role=MessageRole.USER, content="继续核对上下文"),
        Message(role=MessageRole.ASSISTANT, content="好的，已继续"),
    ]


async def _run_compact_path(
    capture: _SeqCapture,
) -> tuple[list[Message], bool, CompressionStats | None]:
    compactor = MemoryCompactor(target_token_budget=1)
    result, success, _ = await compactor.compact(_single_turn_msgs(), capture)
    return result, success, None


async def _run_progressive_chunk_path(
    capture: _SeqCapture,
) -> tuple[list[Message], bool, CompressionStats]:
    # max_chunks=1 → 单块，暴露「ProgressiveCompressor 内部复用 MemoryCompactor 校验」
    compressor = ProgressiveCompressor(max_chunks=1, target_token_budget=1)
    msgs = _make_msgs(_SOURCE_REALITIES, 3, "请分析")
    result, stats = await compressor.compress(msgs, capture)
    return result, stats.progressive_failures == 0, stats


async def _run_progressive_merge_path(
    capture: _SeqCapture,
) -> tuple[list[Message], bool, CompressionStats]:
    compressor = ProgressiveCompressor(max_chunks=2, target_token_budget=1)
    # 多块：块源仅含单实体 filepath，块摘要保留之 → 块 compact 保真通过；
    msgs = _make_msgs(_REALITY_FILE, 7, "请分析")
    result, stats = await compressor.compress(msgs, capture)
    return result, stats.progressive_failures == 0, stats


def _summary_of(result: list[Message]) -> str:
    for m in result:
        if (m.content or "").startswith(SUMMARY_MESSAGE_PREFIX):
            return m.content
    return ""


# ── 1. 实体提取（召回）────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        (f"日志见 {_REALITY_FILE} 第 3 行", _REALITY_FILE),
        ("memory/compaction.py:54 调用", "memory/compaction.py:54"),
        (f"错误 {_REALITY_ERROR}", _REALITY_ERROR),
        (f"函数 {_REALITY_CALL} 返回", _REALITY_CALL),
    ],
)
def test_extract_fidelity_entities_recalls_realities(text: str, expected: str) -> None:
    """契约（.1）：真实形态关键实体必须被提取（路径/行号/错误串/函数调用）。"""
    values = [e.value for e in extract_fidelity_entities(text)]
    assert any(expected in v or v in expected for v in values), (
        f"未提取到 {expected!r}（实际 {values}）"
    )


def test_extract_fidelity_entities_covers_all_kinds() -> None:
    """契约（.2）：混合源文本必须覆盖文件路径/错误串/函数调用/数值四类实体。"""
    entities = extract_fidelity_entities(_SOURCE_REALITIES)
    kinds = {e.kind for e in entities}
    assert _EXPECTED_SOURCE_ENTITIES_KINDS <= kinds, f"缺实体类（实际 {kinds}）"


# ── 2. 保真校验（破坏者视角 + 阈值）──────────────────────────────────────────


def test_check_fidelity_flags_missing_entities() -> None:
    source = f"日志见 {_REALITY_FILE}，错误 {_REALITY_ERROR}"
    report = check_fidelity(source, _omitting_summary())
    assert report.source_entities, "前置：源文本应能提取实体"
    assert report.missing, "摘要遗漏实体必须报 missing"
    assert report.missing_ratio > FIDELITY_RETENTION_THRESHOLD


def test_check_fidelity_passes_when_all_retained() -> None:
    """契约（.4）：摘要保留全部实体 → missing 为空、ratio 不超阈值。"""
    source = f"日志见 {_REALITY_FILE}，错误 {_REALITY_ERROR}"
    report = check_fidelity(source, _full_summary())
    assert not report.missing
    assert report.missing_ratio == 0.0


# ── 3. 三路径接入（族级覆盖）──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "_runner",
    [_run_compact_path, _run_progressive_chunk_path],
    ids=["MemoryCompactor.compact", "ProgressiveCompressor.chunk"],
)
async def test_fidelity_ok_no_retry_no_warning(_runner, caplog) -> None:
    """契约（.5）：摘要保留实体 → 无重试（请求数=1）、无告警、产物用摘要。"""
    caplog.set_level(logging.WARNING)
    capture = _SeqCapture(_full_summary())
    result, success, _ = await _runner(capture)

    assert success is True
    assert len(capture.requests) == 1, "保真足够时不应触发重试"
    assert _summary_of(result).startswith(SUMMARY_MESSAGE_PREFIX)
    assert "compression_fidelity_risk" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "_runner",
    [_run_compact_path, _run_progressive_chunk_path],
    ids=["MemoryCompactor.compact", "ProgressiveCompressor.chunk"],
)
async def test_fidelity_retry_once_then_success(_runner, caplog) -> None:
    """契约（.6）：首摘要遗漏 → 重试一次（请求增 1，重试 prompt 带缺失提示）→
    重试保留实体 → 产物用重试结果、无告警。"""
    caplog.set_level(logging.WARNING)
    capture = _SeqCapture(_omitting_summary(), _full_summary())
    result, success, _ = await _runner(capture)

    assert success is True
    assert len(capture.requests) == 2, "保真不足必须恰好重试一次"
    retry_prompt = "\n".join(str(m.get("content", "")) for m in capture.requests[1])
    assert any(k in retry_prompt for k in ("缺失", "遗漏", "丢失")), (
        f"重试请求必须携带缺失实体提示（实际 {retry_prompt[:200]}）"
    )
    assert _REALITY_FILE in _summary_of(result), "重试成功：产物为第二次摘要（含保留实体）"
    assert "compression_fidelity_risk" not in caplog.text, "重试成功不应告警"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "_runner",
    [_run_compact_path, _run_progressive_chunk_path],
    ids=["MemoryCompactor.compact", "ProgressiveCompressor.chunk"],
)
async def test_fidelity_retry_still_missing_warns(_runner, caplog) -> None:
    """契约（.7，2.17 同步）：连续摘要都遗漏 → 产物仍接受 + 重试上限 2 次
    （共 3 次请求）+ compression_fidelity_risk WARNING。
    注：compact/chunk 路径的保真校验发生在 MemoryCompactor 内部（无 CompressionStats）
    可观测信号为 WARNING + 重试次数；stats 归属见 merge 路径（.8）。"""
    caplog.set_level(logging.WARNING)
    capture = _SeqCapture(_omitting_summary(), _omitting_summary(), _omitting_summary())
    result, success, _ = await _runner(capture)

    assert len(capture.requests) == 3, "应重试 2 次后放弃（审计 2.17：一次即放弃过激）"
    assert success is True, "保真不足不阻断——产物仍接受（有内容优于空摘要）"
    assert "compression_fidelity_risk" in caplog.text, "两次不足必须告警"


@pytest.mark.asyncio
async def test_progressive_merge_path_guards_fidelity(caplog) -> None:
    """
    块摘要保真通过（分块各自校验），final merge 遗漏实体 → 统计 + 告警。"""
    caplog.set_level(logging.WARNING)
    chunk_summary = (
        "## 目标\n- 修复\n\n"
        "## 进度\n### Done\n- [x] 定位\n"
        "### In Progress\n- [ ] 修复\n### Blocked\n- (none)\n\n"
        "## 关键决策\n- **(none)**\n\n"
        "## 下一步\n1. 改造\n\n"
        f"## 关键上下文\n- {_REALITY_FILE}\n"
    )
    capture = _SeqCapture(chunk_summary, chunk_summary, _omitting_summary(), _omitting_summary())
    result, success, stats = await _run_progressive_merge_path(capture)

    assert success is True
    assert _summary_of(result).startswith(SUMMARY_MESSAGE_PREFIX)
    assert stats.fidelity_failures >= 1, "final merge 保真不足必须计入统计"
    assert stats.fidelity_retries >= 1


# ── 4. 协同 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fidelity_failure_does_not_touch_failure_semantics() -> None:
    """契约（.9，协同）：保真不足 ≠ 压缩失败——compact 返回 success=True
    （不误增失败计数），产物仍是结构化摘要消息（非截断兜底）。"""
    capture = _SeqCapture(_omitting_summary(), _omitting_summary())
    compactor = MemoryCompactor(target_token_budget=1)
    result, success, _ = await compactor.compact(_single_turn_msgs(), capture)

    assert success is True, "保真不足不算压缩失败——A-1 失败计数不应误增"
    assert _summary_of(result).startswith(SUMMARY_MESSAGE_PREFIX)


@pytest.mark.asyncio
async def test_llm_crash_keeps_failure_semantics() -> None:
    """契约（.10，破坏者视角/防回归）：LLM 崩溃不许被保真校验改判为成功——
    compact 必须返回 success=False + 失败计数（本次回归教训：曾把崩溃伪装成
    '空摘要保真缺失'致 失败计数/熔断路线失效）。"""
    compactor = MemoryCompactor(target_token_budget=1)

    async def _llm_crash(_messages):
        raise RuntimeError("LLM crash")

    result, success, failcount = await compactor.compact(_single_turn_msgs(), _llm_crash)

    assert success is False, "LLM 崩溃必须保持压缩失败语义（不被保真校验改成功）"
    assert failcount >= 1, "崩溃必须计入 A-1 失败计数"
    assert len(result) == len(_single_turn_msgs()), "失败时应返回原始消息（不截断）"


def test_fidelity_not_derailed_by_number_noise() -> None:
    """契约（.，破坏者视角）：真实高噪声源中续作关键实体（filepath/error/call）
    全部保留、仅部分数值未保留 → 不应判定保真不足。

    若干数字（token 数/端口/PID/批大小等真实必然存在），全局 ratio 即超 0.5 →
    每次真实压缩必触发额外重试 LLM 调用（成本翻倍）+ WARNING 噪音。
    续作脚手架依赖的是 filepath/error/call，不是每个数字；数值降权（宽松阈值 0.7）。
    """
    all_nums = (
        "8080 1024 2048 4096 8192 16384 32768 65536 3001 5000 9001 "
        "6379 5432 443 8081 12000 64000 10001 30000 20000"
    )
    keep_nums = "8080 1024 2048 4096 8192 16384 32768"
    source = f"任务涉及 db/engine.py，调用 validate_ctx()，参数样本：{all_nums}"
    summary = f"任务涉及 db/engine.py，调用 validate_ctx()，参数样本：{keep_nums}"
    report = check_fidelity(source, summary)
    key_missing = [e for e in report.missing if e.kind in ("filepath", "error", "call")]
    num_missing = [e for e in report.missing if e.kind == "number"]
    assert not key_missing, f"前置：关键实体应全部保留（实际缺失 {key_missing}）"
    assert num_missing, "前置：应存在数字缺失（验证对象是数字降噪）"
    assert report.missing_ratio > FIDELITY_RETENTION_THRESHOLD, (
        "前置：全局缺失率须超严格阈值（否则本就不 degraded，无法证回归）"
    )
    assert not report.degraded, "关键实体全保留+仅数字部分缺失不应被判保真不足"


def test_fidelity_key_entity_loss_still_degraded() -> None:
    """契约（.，防过松）：分层阈值降权数字后，关键实体（filepath/call）缺失
    仍必须判保真不足——降噪不能把真正的续作脚手架泄漏也放过。"""
    source = "日志见 logs/a.py，错误 ValueError('x')，函数 validate_ctx()，端口 8080"
    summary = "捕获错误，端口 8080"
    report = check_fidelity(source, summary)
    assert report.degraded, "关键实体（filepath/call）缺失必须判保真不足"


def test_blocked_status_flattened_to_done_is_degraded() -> None:
    """契约（.12，审查项2 破坏者）：源含未完成/阻塞标记，摘要却写成已完成/成功
    （或清空状态）→ status_degraded，degraded 必须为真——续作脚手架据此误判任务
    已收尾是致命失真。"""
    source = "- 任务B：阻塞在外部 API 权限\n- 任务C：等待审批"
    summary = "- 任务B：已完成\n- 任务C：成功交付"
    report = check_fidelity(source, summary)
    assert report.status_degraded, "阻塞/待办被改写成成功必须判语义降级"
    assert report.degraded, "状态改写必须计入整体 degraded"


def test_blocked_status_preserved_is_not_degraded() -> None:
    """契约（.13）：源含未完成标记，摘要仍保留未完成/阻塞标记 → 不判语义降级。"""
    source = "- 任务B：阻塞在外部 API 权限\n- 任务C：等待审批"
    summary = "- 任务B：仍在阻塞（外部 API）\n- 任务C：待审批"
    report = check_fidelity(source, summary)
    assert not report.status_degraded
    assert report.degraded is False, "状态如实保留不判降级"


def test_none_blocker_is_not_active_negative() -> None:
    """契约（.14，防误报）：源/摘要中 `Blocked: (none)` 表示「无阻塞」，不算活跃
    未完成标记——不得误判语义降级（五节模板空阻塞节的合法语境）。"""
    source = "## 进度\n### Blocked\n- (none)\n\n## 关键决策\n- **决策**: 基线\n"
    summary = "## 进度\n### Blocked\n- (none)\n\n## 关键决策\n- **决策**: 基线\n"
    report = check_fidelity(source, summary)
    assert not report.status_degraded, "(none) 无阻塞语境不得误报未完成"


def test_status_retry_hint_guides_restore() -> None:
    """契约（.15，重试引导路径）：状态被改写 → 重试提示必须包含恢复未完成/阻塞
    的指令（即便无结构化实体缺失），引导 LLM 第二轮如实保留状态。"""
    source = "- 任务B：阻塞在外部 API 权限\n- 任务C：等待审批"
    summary = "- 任务B：已完成\n- 任务C：成功交付"
    report = check_fidelity(source, summary)
    hint = build_fidelity_retry_hint(report)
    assert "阻塞" in hint or "pending" in hint.lower(), f"状态缺失必须写入重试提示（实际 {hint!r}）"
    assert "未完成" in hint, "重试提示须提示保留未完成项"


def test_intent_verbs_flattened_is_degraded() -> None:
    """契约（.16，审查项3 破坏者）：源含明确动作动词（修复/实现），摘要却把任务
    动作全部抽象化（如「讨论相关事宜」「进展顺利」）→ intent_degraded，degraded 必为真——
    续作脚手架据此无法恢复「用户要做什么」。"""
    source = "请修复压缩模块的导出缺陷，并实现增量备份"
    summary = "讨论了相关事宜，整体进展顺利，无遗留问题"
    report = check_fidelity(source, summary)
    assert report.intent_degraded, "动作动词全丢必须判意图降级"
    assert report.degraded, "意图丢失必须计入整体 degraded"


def test_intent_verb_preserved_is_not_degraded() -> None:
    """契约（.17）：源含动作动词，摘要保留至少一个 → 不判意图降级。"""
    source = "请修复压缩模块的导出缺陷，并实现增量备份"
    summary = "任务目标：修复导出缺陷；下一步：实现增量备份"
    report = check_fidelity(source, summary)
    assert not report.intent_degraded
    assert report.degraded is False, "动作动词保留不判降级"


def test_pure_status_source_no_intent_false_positive() -> None:
    """契约（.18，防误报）：源为纯状态陈述（无动作动词）→ 意图维度不触发
    （状态维度按 .12~15 独立判定，意图与状态不得互相误伤）。"""
    source = "- 任务B：阻塞在外部 API 权限\n- 任务C：等待审批"
    summary = "- 任务B：已完成\n- 任务C：成功交付"
    report = check_fidelity(source, summary)
    assert not report.intent_degraded, "无动作动词的源不得判意图降级"
    assert report.status_degraded, "状态降级仍须独立成立"


def test_intent_retry_hint_guides_restore() -> None:
    """契约（.19，重试引导路径）：意图丢失 → 重试提示必须包含保留动作动词的指令
    （即便实体/状态都通过），引导 LLM 第二轮恢复任务意图。"""
    source = "请修复压缩模块导出缺陷"
    summary = "讨论了模块问题，进展顺利"
    report = check_fidelity(source, summary)
    hint = build_fidelity_retry_hint(report)
    assert "修复" in hint or "动作" in hint, f"意图缺失必须写入重试提示（实际 {hint!r}）"
