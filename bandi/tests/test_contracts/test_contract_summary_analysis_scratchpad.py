"""功能：契约 — 摘要 <analysis> 草稿块（①：先分析后输出，产物只留正式摘要）

钉的行为：
- 摘要指令要求两步输出（<analysis> 草稿 + <summary> 正式段）与纯文本约束；
- 产物剥离 <analysis> 与 <summary> 标签——只留正式摘要内容；
- 保真校验针对剥离后正文（草稿里的实体不算保留）——遗漏触发重试。
"""

from __future__ import annotations

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compaction import MemoryCompactor, strip_analysis_scratchpad


def test_strip_analysis_block() -> None:
    text = "<analysis>\n草稿 logs/run_0.py\n</analysis>\n<summary>## 目标\n- x</summary>"
    out = strip_analysis_scratchpad(text)
    assert "<analysis>" not in out and "草稿" not in out
    assert "<summary>" not in out
    assert "## 目标" in out


def test_strip_plain_text_unchanged() -> None:
    text = "## 目标\n- x\n\n## 进度"
    assert strip_analysis_scratchpad(text) == text


def test_strip_collapses_extra_blank_lines() -> None:
    text = "<analysis>a</analysis>\n\n\n\n## 目标"
    assert "\n\n\n" not in strip_analysis_scratchpad(text)


def test_summary_backslashes_are_not_regex_templates() -> None:
    text = (
        "<summary>## 关键上下文\n- 路径 C:\\Users\\me\\a.txt\n"
        "- 正则 \\1 与制表 \\t 原样保留</summary>"
    )
    out = strip_analysis_scratchpad(text)
    assert "C:\\Users\\me\\a.txt" in out
    assert "\\1" in out and "\\t" in out


class _SeqCapture:
    """功能：按调用次序回放不同响应（钉「校验发生在剥离后的正文」）。"""

    def __init__(self, responses: list[str]) -> None:
        self.requests: list[list[dict]] = []
        self._responses = responses

    async def __call__(self, messages: list[dict]) -> LLMResponse:
        self.requests.append(messages)
        idx = min(len(self.requests) - 1, len(self._responses) - 1)
        return LLMResponse(
            content=self._responses[idx],
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=5, completion_tokens=3),
            finish_reason="stop",
        )


def _messages_with_path() -> list[Message]:
    return [
        Message(role=MessageRole.SYSTEM, content="你是 Bandi 助手"),
        Message(
            role=MessageRole.USER,
            content="logs/run_0.py 的细节：" + "详细说明。" * 300,
        ),
        Message(role=MessageRole.ASSISTANT, content="已定位"),
        Message(
            role=MessageRole.USER,
            content="logs/run_0.py 的补充：" + "补充说明。" * 300,
        ),
    ]


_GOOD_SUMMARY = (
    "<summary>## 目标\n- 修复 logs/run_0.py\n\n## 进度\n### Done\n- (none)\n"
    "### In Progress\n- (none)\n### Blocked\n- (none)\n\n## 关键决策\n- (none)\n\n"
    "## 下一步\n1. (none)\n\n## 约束与偏好\n- (none)\n\n## 关键上下文\n- (none)</summary>"
)


class TestSummaryRequestAndProduct:
    @pytest.mark.asyncio
    async def test_request_carries_analysis_instruction(self) -> None:
        capture = _SeqCapture([_GOOD_SUMMARY])
        compactor = MemoryCompactor(target_token_budget=10)
        _result, success, _ = await compactor.compact(
            _messages_with_path(), capture, keep_tail=False
        )
        assert success is True
        first_request = "\n".join(str(m.get("content", "")) for m in capture.requests[0])
        assert "<analysis>" in first_request, "摘要请求必须要求分析草稿段"
        assert "<summary>" in first_request, "摘要请求必须要求正式摘要段"

    @pytest.mark.asyncio
    async def test_product_strips_scratchpad(self) -> None:
        draft = (
            "<analysis>\n草稿：涉及 logs/run_0.py，处理了 ValueError\n</analysis>\n" + _GOOD_SUMMARY
        )
        capture = _SeqCapture([draft])
        compactor = MemoryCompactor(target_token_budget=10)
        result, success, _ = await compactor.compact(
            _messages_with_path(), capture, keep_tail=False
        )
        assert success is True
        summaries = [
            m
            for m in result
            if m.role == MessageRole.SYSTEM and (m.content or "").startswith("[上下文摘要]")
        ]
        assert summaries, "产物必须含摘要消息"
        content = summaries[0].content
        assert "<analysis>" not in content and "草稿" not in content
        assert "<summary>" not in content
        assert "## 目标" in content

    @pytest.mark.asyncio
    async def test_fidelity_checks_stripped_body(self) -> None:
        """破坏者视角：草稿含实体但正式段缺 → 校验必须看正式段并触发重试。"""
        only_draft_has_path = (
            "<analysis>\nlogs/run_0.py 细节分析\n</analysis>\n"
            "<summary>## 目标\n- 处理事项\n\n## 进度\n### Done\n- (none)\n"
            "### In Progress\n- (none)\n### Blocked\n- (none)\n\n## 关键决策\n- (none)\n\n"
            "## 下一步\n1. (none)\n\n## 约束与偏好\n- (none)\n\n"
            "## 关键上下文\n- (none)</summary>"
        )
        capture = _SeqCapture([only_draft_has_path, _GOOD_SUMMARY])
        compactor = MemoryCompactor(target_token_budget=10)
        _result, success, _ = await compactor.compact(
            _messages_with_path(), capture, keep_tail=False
        )
        assert success is True
        assert len(capture.requests) >= 2, "正式段缺 filepath 必须触发保真重试（草稿不算）"
