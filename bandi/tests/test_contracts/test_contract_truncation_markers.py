"""功能：契约 — 截断标注族（压缩源头尾帽 + 省略规模标注）

钉的行为：
- ``TokenCounter.truncate_head_tail_to_token_budget``：token 帽下头尾保留（尾部是最新
  内容，不得整体丢弃）、省略标注带省略规模、产物严格 ≤ 预算；
- 压缩源文本截断走头尾（不再只保前缀）——摘要请求必须同时能读到对话开头与结尾；
- ``rule_summary`` 兜底截断与 micro 合并截断的省略标注必须带省略规模（只有「…」= 静默）。
"""

from __future__ import annotations

import re

import pytest

from bandi.core.message import LLMResponse, Message, MessageRole, TokenUsage
from bandi.memory.compaction import MemoryCompactor
from bandi.utils.token_counter import TokenCounter

_OMISSION_RE = re.compile(r"已省略 (\d+) 字符")


def _strip_marker(text: str) -> str:
    return re.sub(r"\n?\.\.\.\[已省略 \d+ 字符\]\.\.\.\n?", "", text)


class _Capture:
    """功能：捕获发给 LLM 的摘要请求，回放结构化摘要响应。"""

    def __init__(self, response_text: str) -> None:
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


_STRUCTURED_SUMMARY = (
    "## 目标\n- x\n\n## 进度\n### Done\n- [x] a\n### In Progress\n- [ ] b\n"
    "### Blocked\n- (none)\n\n## 关键决策\n- **d**: r\n\n## 下一步\n1. n\n\n"
    "## 约束与偏好\n- (none)\n\n## 关键上下文\n- (none)"
)


class TestTruncateHeadTailTokenBudget:
    def test_short_text_unchanged(self) -> None:
        assert TokenCounter.truncate_head_tail_to_token_budget("短文本", 100) == "短文本"

    def test_non_positive_budget_returns_empty(self) -> None:
        assert TokenCounter.truncate_head_tail_to_token_budget("abc", 0) == ""

    def test_head_and_tail_both_preserved(self) -> None:
        text = "头端标记甲" + "中间填充内容。" * 2000 + "尾端标记乙"
        out = TokenCounter.truncate_head_tail_to_token_budget(text, 400)
        assert out.startswith("头端标记甲"), out[:40]
        assert out.endswith("尾端标记乙"), out[-40:]
        assert TokenCounter.count(out) <= 400

    def test_omission_scale_is_accurate(self) -> None:
        text = "甲" * 8000
        out = TokenCounter.truncate_head_tail_to_token_budget(text, 200)
        match = _OMISSION_RE.search(out)
        assert match, out[:200]
        assert int(match.group(1)) == len(text) - len(_strip_marker(out))


class TestCompactionSourceHeadTail:
    @pytest.mark.asyncio
    async def test_source_keeps_head_and_tail(self) -> None:
        capture = _Capture(_STRUCTURED_SUMMARY)
        head = "头端标记甲" + "上文内容。" * 1200
        tail = "下文内容。" * 1200 + "尾端标记乙"
        msgs = [
            Message(role=MessageRole.SYSTEM, content="你是 Bandi 助手"),
            Message(role=MessageRole.USER, content=head),
            Message(role=MessageRole.ASSISTANT, content="收到，继续推进"),
            Message(role=MessageRole.USER, content=tail),
        ]
        compactor = MemoryCompactor(target_token_budget=10)
        _result, success, _ = await compactor.compact(msgs, capture, keep_tail=False)
        assert success is True, "前置：必须真实触发摘要"
        joined = "\n".join(
            str(m.get("content", ""))
            for request in capture.requests
            for m in request
            if m.get("role") == "user"
        )
        assert "头端标记甲" in joined, "压缩源必须保留开头（系统约束/早期对话）"
        assert "尾端标记乙" in joined, "压缩源必须保留结尾（最新对话）——只保前缀是旧病灶"
        assert "已省略" in joined, "触发截断时必须带省略标注"


class TestOmissionScaleMarkers:
    def test_rule_summary_cap_marker_has_scale(self) -> None:
        from bandi.tools.result_summary import _apply_cap

        out = _apply_cap("乙" * 5000)
        assert len(out) <= 1200
        match = _OMISSION_RE.search(out)
        assert match, "兜底截断必须标注省略规模（只有「…」= 静默丢内容）"
        assert int(match.group(1)) == 5000 - len(_strip_marker(out))

    def test_merge_buffer_marker_has_scale(self) -> None:
        from bandi.memory.compression_pipeline import (
            _MAX_MERGED_CONTENT_CHARS,
            _merge_message_buffer,
        )

        buffer = [Message(role=MessageRole.USER, content="短消息" * 200, turn_index=1)]
        merged = _merge_message_buffer(buffer)
        assert merged is not None
        assert len(merged.content or "") <= _MAX_MERGED_CONTENT_CHARS
        match = _OMISSION_RE.search(merged.content or "")
        assert match, "合并超限截断必须标注省略规模"
