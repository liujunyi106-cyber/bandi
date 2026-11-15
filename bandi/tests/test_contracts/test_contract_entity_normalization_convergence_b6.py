"""契约族：`_canonical_subject` 委托 `normalize_entities` 收敛（B6-7c）。

行为不变式（对拍口径——同输入同输出）：
1. 合并命中：LLM 判同 → 返回库中规范名；
2. 未命中/判不同/LLM 失败/无候选 → 原样返回（宁可不合并，不错合并）；
3. 同步与异步 LLM 两种注入形态结果一致；
4. 收敛后生产路径必须经由 `normalize_entities`（单一实现，去内联重复）。
"""

from __future__ import annotations

from typing import Any

import pytest

from bandi.memory.long_term import LongTermMemory, MemoryItem
from bandi.tools.memory_tools import MemoryAssertTool

SUBJECT_NEW = "明远科技"
SUBJECT_CANONICAL = "明远科技公司"


@pytest.fixture()
def long_term(tmp_path: Any) -> Any:
    store = LongTermMemory(str(tmp_path / "lt.db"))
    store.store(
        MemoryItem(
            item_id="triple:seed",
            content=f"{SUBJECT_CANONICAL}·属于·华东区",
            importance=0.6,
            metadata={"subject": SUBJECT_CANONICAL, "predicate": "belongs_to", "object": "华东区"},
        )
    )
    yield store
    store.close()


def _tool(long_term: Any, llm: Any) -> MemoryAssertTool:
    return MemoryAssertTool(long_term=long_term, llm=llm)


class TestCanonicalSubjectConvergence:
    @pytest.mark.asyncio
    async def test_merge_hit_returns_canonical(self, long_term: Any) -> None:
        tool = _tool(long_term, lambda p: '[{"index": 1, "same": true, "reason": "同一家"}]')
        assert await tool._canonical_subject(SUBJECT_NEW) == SUBJECT_CANONICAL

    @pytest.mark.asyncio
    async def test_same_false_keeps_original(self, long_term: Any) -> None:
        tool = _tool(long_term, lambda p: '[{"index": 1, "same": false}]')
        assert await tool._canonical_subject(SUBJECT_NEW) == SUBJECT_NEW

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_original(self, long_term: Any) -> None:
        def _boom(payload: str) -> str:
            raise RuntimeError("llm down")

        tool = _tool(long_term, _boom)
        assert await tool._canonical_subject(SUBJECT_NEW) == SUBJECT_NEW

    @pytest.mark.asyncio
    async def test_no_candidate_never_calls_llm(self, long_term: Any) -> None:
        calls: list[str] = []

        def _spy(payload: str) -> str:
            calls.append(payload)
            return "[]"

        tool = _tool(long_term, _spy)
        assert await tool._canonical_subject("完全不相关的名字") == "完全不相关的名字"
        assert calls == [], "无候选时不得调用 LLM（省一次调用）"

    @pytest.mark.asyncio
    async def test_async_llm_same_result(self, long_term: Any) -> None:
        async def _async_llm(payload: str) -> str:
            return '[{"index": 1, "same": true}]'

        tool = _tool(long_term, _async_llm)
        assert await tool._canonical_subject(SUBJECT_NEW) == SUBJECT_CANONICAL

    @pytest.mark.asyncio
    async def test_delegates_to_normalize_entities(
        self, long_term: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """收敛：生产路径必须经由 `normalize_entities`（单一实现）。"""
        from bandi.memory import entity_normalization as norm_mod
        from bandi.memory.entity_normalization import MergePlan

        calls: list[tuple[list[str], list[str]]] = []

        async def _spy(new_names: list[str], existing_names: list[str], *, llm: Any) -> MergePlan:
            calls.append((list(new_names), list(existing_names)))
            return MergePlan(merges=[(SUBJECT_NEW, SUBJECT_CANONICAL)], kept=[])

        monkeypatch.setattr(norm_mod, "normalize_entities", _spy)
        tool = _tool(long_term, lambda p: "[]")
        assert await tool._canonical_subject(SUBJECT_NEW) == SUBJECT_CANONICAL
        assert calls and calls[0][0] == [SUBJECT_NEW], "须经 normalize_entities 编排"

    @pytest.mark.asyncio
    async def test_end_to_end_assert_uses_canonical(self, long_term: Any) -> None:
        """端到端：断言入口的存储主体 = 规范名，且留 normalized_from 追溯。"""
        tool = _tool(long_term, lambda p: '[{"index": 1, "same": true}]')
        result = await tool.execute(
            {"subject": SUBJECT_NEW, "predicate": "belongs_to", "object": "华南区"}
        )
        assert result.get("asserted") is True, result
        stored = [it for it in long_term.list_items() if "华南区" in it.content]
        assert stored and stored[0].metadata.get("subject") == SUBJECT_CANONICAL
        assert stored[0].metadata.get("normalized_from") == SUBJECT_NEW
