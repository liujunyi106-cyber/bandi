from __future__ import annotations

from bandi.core.message import LLMResponse
from bandi.eval.hallucination_checker import (
    HallucinationChecker,
    _parse_json_output,
    check_hallucination,
    extract_candidates,
    l1_check,
    l2_check,
)
from bandi.llm.mock import MockProvider
from bandi.ontology.lookup import OntologyEntityLookup
from bandi.ontology.spec import open_ontology
from bandi.ontology.triple_store import OntologyStore


class TestParseJsonOutput:
    def test_json_marked_block_wins_over_plain_block(self) -> None:
        """#17：有 ```json 标记的块优先于无标记块（无标记块内容恰好也是 JSON 时不得被误采）。"""
        text = (
            '```\n{"hallucination_score": 0.0, "hallucination_spans": []}\n```\n'
            '```json\n{"hallucination_score": 0.9, "hallucination_spans": ["made up"]}\n```'
        )
        result = _parse_json_output(text)
        assert result.get("hallucination_score") == 0.9

    def test_json_marker_variants(self) -> None:
        """#17：json 标记兼容大小写与空格变体。"""
        for marker in ("json", "JSON", "json "):
            text = f'```{marker}\n{{"hallucination_score": 0.5}}\n```'
            result = _parse_json_output(text)
            assert result.get("hallucination_score") == 0.5


class TestExtractCandidates:
    def test_extracts_capitalized_names(self) -> None:
        result = extract_candidates("Alice met Bob at the Conference Center")
        assert "Alice" in result
        assert "Bob" in result
        assert "Conference Center" in result

    def test_skips_common_words(self) -> None:
        result = extract_candidates("This is a Test Project for the API")
        assert "Test Project" in result
        assert "This" not in result
        assert "API" not in result

    def test_empty_text(self) -> None:
        assert extract_candidates("") == []


class TestChineseCandidates:
    def test_extracts_chinese_entity_candidates(self) -> None:
        result = extract_candidates("张三和李四讨论了购买杂货")
        assert "张三" in result
        assert "购买杂货" in result

    def test_skips_common_chinese_stopwords(self) -> None:
        result = extract_candidates("我们可以走了")
        # 停用词本身必须被过滤
        assert "我们" not in result
        assert "可以" not in result
        # 纯停用词组合（相邻 2 字子串全为停用词）不得作为实体候选
        assert "我们可以" not in result


def _make_lookup(*labels):
    """功能：造含指定 label 实体的三元组本体并返回只读查询适配（按 label 查实体的替身）。"""
    import tempfile
    from pathlib import Path

    store = OntologyStore(open_ontology(str(Path(tempfile.mkdtemp()) / "o.db")))
    store.define_class({"id": "Task"}, "T00:00:00")
    for index, label in enumerate(labels):
        store.assert_entity({"id": f"e{index}", "classes": ["Task"], "label": label}, "T00:00:00")
    return OntologyEntityLookup(store)


def _lookup(tmp_path, *labels):
    return _make_lookup(*labels)


class TestL1Check:
    def test_existing_entity_found(self, tmp_path) -> None:
        import asyncio

        lookup = _lookup(tmp_path, "Buy Groceries")
        report = asyncio.run(l1_check("Buy Groceries is pending", lookup))
        assert "Buy Groceries" in report.found_entities
        assert report.confidence_delta == 0.0

    def test_missing_entity_detected(self, tmp_path) -> None:
        import asyncio

        lookup = _lookup(tmp_path, "Real Task")
        report = asyncio.run(l1_check("Fake Document is due", lookup))
        assert "Fake Document" in report.missing_entities
        assert report.confidence_delta > 0.0

    def test_no_candidates_returns_empty(self, tmp_path) -> None:
        import asyncio

        lookup = _lookup(tmp_path)
        report = asyncio.run(l1_check("hello world this is fine", lookup))
        assert report.confidence_delta == 0.0

    def test_chinese_entity_found_and_missing(self, tmp_path) -> None:
        import asyncio

        lookup = _lookup(tmp_path, "购买杂货")
        report = asyncio.run(l1_check("完成了购买杂货，但幻影项目未启动", lookup))
        assert "购买杂货" in report.found_entities
        assert report.confidence_delta > 0.0

    def test_plain_chinese_reply_not_blocked(self, tmp_path) -> None:
        import asyncio

        lookup = _lookup(tmp_path)
        report = asyncio.run(l1_check("Mock回复: 你好\n\n*此内容由 AI 生成，仅供参考*", lookup))
        assert report.confidence_delta < 0.7


class TestL2Check:
    def test_consistent_output_low_score(self) -> None:
        import asyncio

        provider = MockProvider()
        provider.add_response(
            LLMResponse(content='{"hallucination_score": 0.0, "hallucination_spans": []}')
        )
        report = asyncio.run(l2_check("The sky is blue", "Context: sky color", provider))
        assert report.hallucination_score == 0.0

    def test_inconsistent_output_high_score(self) -> None:
        import asyncio

        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content='{"hallucination_score": 0.8, "hallucination_spans": ["Earth is flat"]}'
            )
        )
        report = asyncio.run(l2_check("Earth is flat", "Context: Earth is round", provider))
        assert report.hallucination_score >= 0.7
        assert len(report.hallucination_spans) > 0


class TestCheckHallucination:
    def test_normal_text_passes(self) -> None:
        import asyncio

        graph = _make_lookup("My Task")
        provider = MockProvider()
        provider.add_response(
            LLMResponse(content='{"hallucination_score": 0.0, "hallucination_spans": []}')
        )
        report = asyncio.run(
            check_hallucination(
                text="My Task is done",
                graph=graph,
                provider=provider,
                context="Context about My Task",
            )
        )
        assert not report.blocked
        assert report.overall_score <= 0.7

    def test_sf04_non_existent_entity(self) -> None:
        graph = _make_lookup("Known Task")
        import asyncio

        report = asyncio.run(check_hallucination(text="Fake Item does not exist", graph=graph))
        assert report.l1 is not None
        assert len(report.l1.missing_entities) > 0
        assert report.l1.confidence_delta > 0.0

    def test_sf05_fact_inconsistency_blocks(self) -> None:
        import asyncio

        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content='{"hallucination_score": 0.9, "hallucination_spans": ["3 tasks total"]}'
            )
        )
        report = asyncio.run(
            check_hallucination(
                text="There are 3 tasks total",
                context="The file clearly states there are exactly 5 tasks.",
                provider=provider,
                l2_threshold=0.0,
            )
        )
        assert report.blocked
        assert report.overall_score > 0.7

    def test_fuzzy_entity_l1_pass_l2_low(self) -> None:
        import asyncio

        graph = _make_lookup("Quarterly Report Prep")
        provider = MockProvider()
        provider.add_response(
            LLMResponse(content='{"hallucination_score": 0.3, "hallucination_spans": []}')
        )
        report = asyncio.run(
            check_hallucination(
                text="Quarterly Report needs review at Conference Room A",
                graph=graph,
                provider=provider,
                context="Some conversational context about reports",
                l2_threshold=0.0,
            )
        )
        assert report.l1 is not None
        assert (
            "Quarterly" in report.l1.found_entities
            or "Quarterly Report" in report.l1.found_entities
        )
        assert report.l1.confidence_delta <= 0.3
        assert report.l2 is not None
        assert report.l2.hallucination_score < 0.7
        assert not report.blocked

    def test_context_triggers_l2_even_when_graph_present(self) -> None:
        """#8：graph 存在且 未超阈值时，只要有 context 也应触发 深度校验。"""
        import asyncio

        graph = _make_lookup("My Task")
        provider = MockProvider()
        provider.add_response(
            LLMResponse(
                content='{"hallucination_score": 0.85, "hallucination_spans": ["wrong fact"]}'
            )
        )
        report = asyncio.run(
            check_hallucination(
                text="My Task is done",
                graph=graph,
                provider=provider,
                context="Context reveals a contradicting fact",
                l2_threshold=0.9,
            )
        )
        assert report.l2 is not None
        assert report.l2.hallucination_score >= 0.7


class TestHallucinationChecker:
    def test_checker_wraps_check_hallucination(self) -> None:
        import asyncio

        graph = _make_lookup("Alpha Project")
        provider = MockProvider()
        provider.add_response(
            LLMResponse(content='{"hallucination_score": 0.0, "hallucination_spans": []}')
        )
        checker = HallucinationChecker(graph=graph, provider=provider)
        report = asyncio.run(checker.check(text="Alpha Project is progressing well"))
        assert report.l1 is not None
        assert "Alpha Project" in report.l1.found_entities
        assert not report.blocked

    def test_checker_inherits_thresholds(self) -> None:
        import asyncio

        graph = _make_lookup("Real Project")
        provider = MockProvider()
        provider.add_response(
            LLMResponse(content='{"hallucination_score": 0.0, "hallucination_spans": []}')
        )
        checker = HallucinationChecker(
            graph=graph, provider=provider, block_threshold=0.05, l2_threshold=0.0
        )
        report = asyncio.run(checker.check(text="Mysterious Project Omega"))
        assert report.l1 is not None
        assert "Mysterious Project Omega" in report.l1.missing_entities
        assert report.blocked

    def test_checker_no_graph_fallback(self) -> None:
        import asyncio

        provider = MockProvider()
        provider.add_response(
            LLMResponse(content='{"hallucination_score": 0.0, "hallucination_spans": []}')
        )
        checker = HallucinationChecker(provider=provider)
        report = asyncio.run(checker.check(text="Hello world"))
        assert report.l1 is None
        assert not report.blocked
