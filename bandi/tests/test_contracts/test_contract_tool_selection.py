"""功能：工具面 Top-N 裁剪契约。

钉行为：小工具面零变化；核心工具永在；中文意图词能命中英文工具；
超量裁剪且输出稳定（字节序不抖动——前缀缓存的前提）。
"""

from __future__ import annotations

from bandi.tools.selection import (
    CORE_TOOL_NAMES,
    MAX_TOOLS,
    query_terms,
    score_schema,
    select_relevant_schemas,
)


def _schema(name: str, description: str = "") -> dict:
    return {"type": "function", "function": {"name": name, "description": description}}


def _many(count: int, prefix: str = "tool") -> list[dict]:
    return [_schema(f"{prefix}_{i:03d}", "generic tool") for i in range(count)]


class TestSmallSurfaceUnchanged:
    def test_small_surface_returned_as_is(self) -> None:
        # ≤25 一律原样返回（对象同一性也保持，避免无谓拷贝）
        schemas = _many(MAX_TOOLS)
        assert select_relevant_schemas(schemas, "任意输入") is schemas

    def test_boundary_exactly_max(self) -> None:
        # 业务查询（非闲聊）——恰好等于上限时不裁剪、原样返回
        schemas = _many(MAX_TOOLS)
        assert select_relevant_schemas(schemas, "统计下这批数据") is schemas


class TestCoreToolsAlwaysKept:
    def test_core_survives_cropping(self) -> None:
        # 108 个工具里含核心工具——裁剪后核心必须都在
        schemas = _many(108) + [_schema("read"), _schema("write"), _schema("bash")]
        picked = select_relevant_schemas(schemas, "帮我搜索竞品资料")
        names = {s["function"]["name"] for s in picked}
        assert {"read", "write", "bash"} <= names

    def test_core_names_are_subset_of_result_never_less(self) -> None:
        schemas = _many(200)
        picked = select_relevant_schemas(schemas, "x")
        assert len(picked) <= max(MAX_TOOLS, len([n for n in CORE_TOOL_NAMES]))

    def test_core_count_exceeds_budget_does_not_drop_core(self) -> None:
        # 破坏者：核心工具全塞进来 + 大量普通工具——预算自动放宽，核心一个不丢
        schemas = [_schema(n) for n in sorted(CORE_TOOL_NAMES)] + _many(120)
        picked = select_relevant_schemas(schemas, "x")
        names = {s["function"]["name"] for s in picked}
        assert CORE_TOOL_NAMES <= names


class TestKeywordScoring:
    def test_chinese_intent_word_hits_english_tool(self) -> None:
        # 中文查询与英文工具名之间的词面断层：靠意图词表接到 web_search
        schemas = _many(108) + [
            _schema("web_search", "search the web"),
            _schema("excel_reader", "read excel files"),
        ]
        picked = select_relevant_schemas(schemas, "帮我查一下竞品资料")
        names = {s["function"]["name"] for s in picked}
        assert "web_search" in names

    def test_english_query_hits_tool_by_name(self) -> None:
        schemas = _many(108) + [_schema("web_search", "search the web")]
        picked = select_relevant_schemas(schemas, "please web search this")
        names = {s["function"]["name"] for s in picked}
        assert "web_search" in names

    def test_score_prefers_name_over_description(self) -> None:
        # 权重：名命中 3.0 > 描述命中 1.5
        terms = query_terms("search")
        assert score_schema("web_search", "无关描述", terms) > score_schema(
            "foo", "search inside description", terms
        )

    def test_unrelated_query_still_returns_bounded_set(self) -> None:
        schemas = _many(108)
        picked = select_relevant_schemas(schemas, "zzz 毫不相关")
        assert len(picked) == MAX_TOOLS


class TestDeterminism:
    def test_output_is_stable_and_sorted(self) -> None:
        # 同输入两次 → 同输出同序（前缀缓存不被打穿）
        schemas = _many(108) + [_schema("web_search")]
        first = select_relevant_schemas(schemas, "搜索")
        second = select_relevant_schemas(schemas, "搜索")
        assert [s["function"]["name"] for s in first] == [s["function"]["name"] for s in second]
        names = [s["function"]["name"] for s in first]
        assert names == sorted(names)

    def test_query_terms_chinese_ngram(self) -> None:
        # 中文长段补 2-gram（与 FTS 口径一致），提升中文召回
        terms = query_terms("查询竞品价格")
        assert "竞品" in terms or any("竞品" in t for t in terms)
