"""契约：实体归一两阶段（粗筛 + LLM 精判）。

datafoundry DataLink 的两阶段归一（`llm_mapper.py:594-609`）：廉价粗筛把
O(n²) 压到 O(k)，LLM 逐对精判，**相似度只决定「值不值得问」，不下合并结论**
（datafoundry prompt 原文第 6 条："High similarity does NOT guarantee they
should be merged"）。

核心风险是**误合并不可逆**——把两个事实永久粘在一起。本契约重点钉降级路径：
LLM 失败/胡说/漏判时，一律按「不合并」处理。
"""

from __future__ import annotations

import pytest

from bandi.memory.entity_normalization import (
    build_judge_payload,
    candidate_pairs,
    normalize_entities,
    parse_judgement,
)


class TestPrefilter:
    def test_containment_becomes_candidate(self) -> None:
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        assert c and c[0].similarity >= 0.7

    def test_unrelated_not_candidate(self) -> None:
        assert candidate_pairs(["订单服务"], ["结算中心"]) == []

    def test_exact_duplicates_skipped(self) -> None:
        """同名不需要问 LLM——它必然该合并，直接跳过省一次调用。"""
        assert candidate_pairs(["订单服务"], ["订单服务"]) == []

    def test_similar_but_different_domain_not_candidate(self) -> None:
        """破坏者视角：都叫「服务」但不是一回事，不该进候选。"""
        assert candidate_pairs(["订单服务"], ["支付服务"]) == []

    def test_candidates_sorted_by_similarity(self) -> None:
        """破坏者视角：完全相同的对被跳过，剩下的按相似度降序供 LLM 判定。"""
        c = candidate_pairs(["报告生成器"], ["报告生成器v2", "报告生成器"])
        # 完全相同的一对不进候选；只剩包含关系那一对
        assert [(x.new, x.existing) for x in c] == [("报告生成器", "报告生成器v2")]
        assert c[0].similarity >= 0.7

    def test_candidate_count_bounded(self) -> None:
        names = [f"服务{i}" for i in range(50)]
        assert len(candidate_pairs(names, names + ["服务X"], max_candidates=10)) <= 10


class TestJudgeParsing:
    def test_only_explicit_true_is_merged(self) -> None:
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        plan = parse_judgement('[{"index": 1, "same": true, "reason": "同一家公司"}]', c)
        assert plan.merges == [("明远科技", "明远科技公司")]

    def test_garbage_output_merges_nothing(self) -> None:
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        assert parse_judgement("抱歉，我无法完成", c).merges == []

    def test_truncated_json_merges_nothing(self) -> None:
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        assert parse_judgement('[{"index": 1, "same": tr', c).merges == []

    def test_missing_index_merges_nothing(self) -> None:
        """破坏者视角：LLM 只答了一半——没答的那一半按不合并。"""
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        plan = parse_judgement("[]", c)
        assert plan.merges == []
        assert plan.rejected_candidates

    def test_string_index_accepted(self) -> None:
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        plan = parse_judgement('[{"index": "1", "same": true}]', c)
        assert plan.merges == [("明远科技", "明远科技公司")]

    def test_payload_lists_pairs_with_numbers(self) -> None:
        c = candidate_pairs(["明远科技"], ["明远科技公司"])
        payload = build_judge_payload(c)
        assert "1 | 明远科技 | 明远科技公司" in payload
        assert "同一个现实事物" in payload


class TestNormalizeOrchestration:
    @pytest.mark.asyncio
    async def test_non_candidate_names_are_kept(self) -> None:
        """破坏者视角：粗筛没放行的名字，绝不能因为「没进候选」就消失。"""
        plan = await normalize_entities(
            ["明远科技", "支付服务"],
            ["明远科技公司"],
            llm=lambda p: '[{"index": 1, "same": true}]',
        )
        assert plan.merges == [("明远科技", "明远科技公司")]
        assert "支付服务" in plan.kept

    @pytest.mark.asyncio
    async def test_llm_failure_keeps_everything(self) -> None:
        plan = await normalize_entities(
            ["明远科技", "支付服务"],
            ["明远科技公司"],
            llm=lambda p: "无法处理",
        )
        assert plan.merges == []
        assert set(plan.kept) == {"明远科技", "支付服务"}

    @pytest.mark.asyncio
    async def test_async_llm_supported(self) -> None:
        async def llm(payload: str) -> str:
            return '[{"index": 1, "same": false}]'

        plan = await normalize_entities(["明远科技"], ["明远科技公司"], llm=llm)
        assert plan.merges == []

    @pytest.mark.asyncio
    async def test_no_candidates_skips_llm(self) -> None:
        calls: list[str] = []

        def llm(payload: str) -> str:
            calls.append(payload)
            return "[]"

        plan = await normalize_entities(["完全不同的东西"], ["毫不相干"], llm=llm)
        assert calls == [], "没有候选时不应调用 LLM"
        assert plan.kept == ["完全不同的东西"]

    @pytest.mark.asyncio
    async def test_llm_reject_keeps_both(self) -> None:
        plan = await normalize_entities(
            ["订单服务"],
            ["订单服务v2"],
            llm=lambda p: '[{"index": 1, "same": false, "reason": "版本不同"}]',
        )
        assert plan.merges == []
