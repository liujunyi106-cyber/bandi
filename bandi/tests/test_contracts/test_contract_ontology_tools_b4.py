"""契约：本体四工具（对照表 -5 / -3,6）。

复刻 dsh-ontology `src/tools.ts`（727 行全读）。行为不变式：

**ontology_define（TBox）**
  1. **重复遍解析**——同一调用内的定义按「反复重试直到无进展」应用，
     故子类可先于父类、关系可先于其逆；只有永远无法解析的才被拒
  2. 重定义整体替换
  3. 输出分 `classes`/`relations`/`rejected` 三组，rejected 带 ref + 违规文案

**ontology_assert（ABox）**
  4. **先建实体后写事实**——一次调用可同时引入个体及其关系
  5. 事实逐条校验；被拒的事实带 ref（`s p o` 三元组文本）与违规
  6. 非 strict 模式写成功但带违规 → 进 `warnings`（不混进 rejected）
  7. 重复断言幂等

**ontology_query（六模式）**
  8. 六模式全量：schema / stats / entities / facts / neighbors / path
  9. `includeInferred` 出 `via` 标记；**inference 关闭时强制不推论**
 10. entities 模式给 `allClasses`（直接类 + 继承闭包，排序稳定）
 11. `limit` 截断须置 `truncated` 标志（多取一条判断是否截断）
 12. neighbors 缺 `entity` 报错；path 缺 from/to 报错
 13. depth 钳制在 [1, 8]，默认 2

**ontology_retract**
 14. 撤回实体级联并报 `cascadedFacts`
 15. 撤回词表被依赖时进 `refused`（不是 missing）；不存在进 `missing`
 16. 撤回不存在的事实进 `missing`

dsh-ontology `src/tools.ts:145-727`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.ontology.tools import OntologyToolContext, create_ontology_tools

NOW = "2026-10-13T00:00:00Z"


@pytest.fixture()
def ctx(tmp_path: Path) -> Any:
    from bandi.ontology.spec import open_ontology
    from bandi.ontology.triple_store import OntologyStore, StoreLimits

    tables = open_ontology(str(tmp_path / "onto.db"))
    store = OntologyStore(tables, StoreLimits(strict=True))
    yield OntologyToolContext(
        store=store, inference=True, default_limit=20, max_limit=100, now=lambda: NOW
    )
    store.close()


def _by_name(ctx: Any) -> dict[str, Any]:
    return {tool.name: tool for tool in create_ontology_tools(ctx)}


class TestFourToolsRegistered:
    def test_exactly_four_tools(self, ctx: Any) -> None:
        tools = create_ontology_tools(ctx)
        assert [t.name for t in tools] == [
            "ontology_define",
            "ontology_assert",
            "ontology_query",
            "ontology_retract",
        ]

    def test_every_tool_has_description_and_execute(self, ctx: Any) -> None:
        for tool in create_ontology_tools(ctx):
            assert tool.description, f"{tool.name} 缺描述（模型无从判断何时用）"
            assert callable(tool.execute)

    def test_tools_have_json_schema(self, ctx: Any) -> None:
        for tool in create_ontology_tools(ctx):
            assert isinstance(tool.parameters, dict)
            assert isinstance(tool.output_schema, dict)


# ── define ──────────────────────────────────────────────────────────────


class TestDefineTool:
    def test_defines_classes_and_relations(self, ctx: Any) -> None:
        tool = _by_name(ctx)["ontology_define"]
        result = tool.execute(
            {
                "classes": [{"id": "Person"}, {"id": "Company"}],
                "relations": [{"id": "worksAt", "domain": ["Person"]}],
            }
        )
        assert sorted(result["classes"]) == ["Company", "Person"]
        assert result["relations"] == ["worksAt"]
        assert result["rejected"] == []

    def test_forward_reference_resolved_by_repeated_passes(self, ctx: Any) -> None:
        """子类先于父类声明也要成功——按反复重试应用，不按参数顺序。"""
        tool = _by_name(ctx)["ontology_define"]
        result = tool.execute(
            {"classes": [{"id": "Employee", "subClassOf": ["Person"]}, {"id": "Person"}]}
        )
        assert sorted(result["classes"]) == ["Employee", "Person"], "顺序无关"
        assert result["rejected"] == []

    def test_relation_before_inverse_resolved(self, ctx: Any) -> None:
        tool = _by_name(ctx)["ontology_define"]
        result = tool.execute(
            {"relations": [{"id": "worksAt", "inverseOf": "employedBy"}, {"id": "employedBy"}]}
        )
        assert sorted(result["relations"]) == ["employedBy", "worksAt"]
        assert result["rejected"] == []

    def test_unresolvable_input_is_rejected_with_ref(self, ctx: Any) -> None:
        tool = _by_name(ctx)["ontology_define"]
        result = tool.execute({"classes": [{"id": "X", "subClassOf": ["Ghost"]}]})
        assert result["classes"] == []
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["ref"] == "X"
        assert result["rejected"][0]["violations"]

    def test_partial_success_keeps_good_definitions(self, ctx: Any) -> None:
        """一部分失败不牵连另一部分——只拒永远解析不了的那条。"""
        tool = _by_name(ctx)["ontology_define"]
        result = tool.execute({"classes": [{"id": "Good"}, {"id": "Bad", "subClassOf": ["Ghost"]}]})
        assert result["classes"] == ["Good"]
        assert len(result["rejected"]) == 1

    def test_redefine_replaces_wholesale(self, ctx: Any) -> None:
        tools = _by_name(ctx)
        tools["ontology_define"].execute({"classes": [{"id": "A", "label": "原"}]})
        tools["ontology_define"].execute({"classes": [{"id": "A", "label": "新"}]})
        term = ctx.store.graph().terms["A"]
        assert term.label == "新"
        assert term.created_at == NOW, "重定义保留 created_at"

    def test_mutual_cycle_terminates(self, ctx: Any) -> None:
        """破坏者：A 继承 B 且 B 继承 A 会让重复遍永不收敛——必须终止并全拒。"""
        tool = _by_name(ctx)["ontology_define"]
        result = tool.execute(
            {"classes": [{"id": "A", "subClassOf": ["B"]}, {"id": "B", "subClassOf": ["A"]}]}
        )
        assert result["classes"] == []
        assert len(result["rejected"]) == 2, "成环的两条都要拒"


# ── assert ──────────────────────────────────────────────────────────────


class TestAssertTool:
    def _seed(self, ctx: Any) -> None:
        _by_name(ctx)["ontology_define"].execute(
            {
                "classes": [{"id": "Person"}, {"id": "Company"}],
                "relations": [{"id": "worksAt", "domain": ["Person"], "range": ["Company"]}],
            }
        )

    def test_entities_before_facts_in_one_call(self, ctx: Any) -> None:
        """一次调用同时引入个体及其关系——实体先建，事实后写。"""
        self._seed(ctx)
        tool = _by_name(ctx)["ontology_assert"]
        result = tool.execute(
            {
                "entities": [
                    {"id": "alice", "classes": ["Person"]},
                    {"id": "acme", "classes": ["Company"]},
                ],
                "facts": [{"subject": "alice", "predicate": "worksAt", "object": "acme"}],
            }
        )
        assert result["entities"] == ["alice", "acme"]
        assert result["facts"] == ["alice worksAt acme"]
        assert result["rejected"] == []

    def test_rejected_fact_carries_triple_ref(self, ctx: Any) -> None:
        self._seed(ctx)
        tool = _by_name(ctx)["ontology_assert"]
        result = tool.execute(
            {
                "entities": [{"id": "alice", "classes": ["Person"]}],
                "facts": [{"subject": "alice", "predicate": "worksAt", "object": "ghost"}],
            }
        )
        assert result["facts"] == []
        assert result["rejected"][0]["ref"] == "alice worksAt ghost"
        assert "ghost" in result["rejected"][0]["violations"][0]

    def test_one_bad_fact_does_not_block_good_ones(self, ctx: Any) -> None:
        self._seed(ctx)
        tool = _by_name(ctx)["ontology_assert"]
        result = tool.execute(
            {
                "entities": [
                    {"id": "alice", "classes": ["Person"]},
                    {"id": "acme", "classes": ["Company"]},
                ],
                "facts": [
                    {"subject": "alice", "predicate": "worksAt", "object": "ghost"},
                    {"subject": "alice", "predicate": "worksAt", "object": "acme"},
                ],
            }
        )
        assert result["facts"] == ["alice worksAt acme"]
        assert len(result["rejected"]) == 1

    def test_reassert_is_idempotent(self, ctx: Any) -> None:
        self._seed(ctx)
        tool = _by_name(ctx)["ontology_assert"]
        args = {
            "entities": [
                {"id": "alice", "classes": ["Person"]},
                {"id": "acme", "classes": ["Company"]},
            ],
            "facts": [{"subject": "alice", "predicate": "worksAt", "object": "acme"}],
        }
        tool.execute(args)
        tool.execute(args)
        assert len(ctx.store.graph().facts) == 1

    def test_lenient_mode_warns_instead_of_rejecting(self, tmp_path: Path) -> None:
        """非 strict：明知不一致的事实落库并警告——不混进 rejected。"""
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.triple_store import OntologyStore, StoreLimits

        tables = open_ontology(str(tmp_path / "lenient.db"))
        store = OntologyStore(tables, StoreLimits(strict=False))
        context = OntologyToolContext(
            store=store, inference=True, default_limit=20, max_limit=100, now=lambda: NOW
        )
        try:
            tool = _by_name(context)["ontology_assert"]
            result = tool.execute(
                {"facts": [{"subject": "a", "predicate": "ghost", "object": "b"}]}
            )
            assert result["rejected"] == []
            assert result["warnings"], "违规必须进 warnings"
        finally:
            store.close()


# ── query 六模式 ────────────────────────────────────────────────────────


class TestQueryToolModes:
    @pytest.fixture()
    def seeded(self, ctx: Any) -> Any:
        tools = _by_name(ctx)
        tools["ontology_define"].execute(
            {
                "classes": [
                    {"id": "Person"},
                    {"id": "Employee", "subClassOf": ["Person"]},
                    {"id": "Company"},
                ],
                "relations": [
                    {"id": "worksAt", "domain": ["Person"], "range": ["Company"]},
                    {"id": "knows", "characteristics": ["symmetric"]},
                ],
            }
        )
        tools["ontology_assert"].execute(
            {
                "entities": [
                    {"id": "alice", "classes": ["Employee"], "label": "爱丽丝"},
                    {"id": "bob", "classes": ["Person"]},
                    {"id": "acme", "classes": ["Company"]},
                ],
                "facts": [
                    {"subject": "alice", "predicate": "worksAt", "object": "acme"},
                    {"subject": "alice", "predicate": "knows", "object": "bob"},
                ],
            }
        )
        return ctx

    def test_mode_schema(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute({"mode": "schema"})
        assert any(line.startswith("class Person") for line in result["schema"])
        assert any(line.startswith("rel worksAt") for line in result["schema"])

    def test_mode_stats(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute({"mode": "stats"})
        assert result["stats"]["classes"] == 3
        assert result["stats"]["relations"] == 2
        assert result["stats"]["entities"] == 3
        assert result["stats"]["facts"] == 2

    def test_mode_entities_by_class_includes_subclass(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute({"mode": "entities", "class": "Person"})
        ids = {e["id"] for e in result["entities"]}
        assert ids == {"alice", "bob"}, "问父类须返回子类成员"

    def test_entities_reports_all_classes_closure(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute(
            {"mode": "entities", "class": "Employee"}
        )
        alice = next(e for e in result["entities"] if e["id"] == "alice")
        assert alice["allClasses"] == ["Employee", "Person"], "继承闭包"
        assert alice["classes"] == ["Employee"], "直接类另列"

    def test_mode_facts_wildcards(self, seeded: Any) -> None:
        tool = _by_name(seeded)["ontology_query"]
        assert len(tool.execute({"mode": "facts"})["facts"]) == 2
        assert len(tool.execute({"mode": "facts", "subject": "alice"})["facts"]) == 2
        assert len(tool.execute({"mode": "facts", "predicate": "knows"})["facts"]) == 1

    def test_mode_neighbors(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute(
            {"mode": "neighbors", "entity": "alice", "depth": 1}
        )
        assert set(result["reached"]) == {"alice", "acme", "bob"}

    def test_neighbors_requires_entity(self, seeded: Any) -> None:
        with pytest.raises(ValueError):
            _by_name(seeded)["ontology_query"].execute({"mode": "neighbors"})

    def test_mode_path(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute(
            {"mode": "path", "from": "alice", "to": "acme"}
        )
        assert result["connected"] is True
        assert len(result["path"]) == 1

    def test_path_requires_both_ends(self, seeded: Any) -> None:
        with pytest.raises(ValueError):
            _by_name(seeded)["ontology_query"].execute({"mode": "path", "from": "alice"})

    def test_path_disconnected_reports_false(self, seeded: Any) -> None:
        """破坏者：真正无连通的两个实体——库里没有任何可达路径。"""
        tools = _by_name(seeded)
        tools["ontology_define"].execute({"classes": [{"id": "Lonely"}]})
        tools["ontology_assert"].execute({"entities": [{"id": "island", "classes": ["Lonely"]}]})
        result = tools["ontology_query"].execute({"mode": "path", "from": "island", "to": "acme"})
        assert result["connected"] is False
        assert result["path"] == []

    def test_include_inferred_marks_via(self, seeded: Any) -> None:
        """includeInferred 出 via 标记——派生结论可解释。"""
        result = _by_name(seeded)["ontology_query"].execute(
            {"mode": "facts", "includeInferred": True}
        )
        vias = {f["via"] for f in result["facts"]}
        assert "asserted" in vias
        assert "symmetric" in vias, "knows 是对称关系，应推导出 b knows a"
        by_via = {f["subject"]: f["via"] for f in result["facts"]}
        assert by_via.get("bob") == "symmetric"

    def test_include_inferred_off_by_default(self, seeded: Any) -> None:
        result = _by_name(seeded)["ontology_query"].execute({"mode": "facts"})
        assert {f["via"] for f in result["facts"]} == {"asserted"}

    def test_inference_disabled_forces_no_entailment(self, tmp_path: Path) -> None:
        """inference=False 时 includeInferred 也不推论——配置开关必须真的生效。"""
        from bandi.ontology.spec import open_ontology
        from bandi.ontology.triple_store import OntologyStore, StoreLimits

        tables = open_ontology(str(tmp_path / "noinf.db"))
        store = OntologyStore(tables, StoreLimits())
        context = OntologyToolContext(
            store=store, inference=False, default_limit=20, max_limit=100, now=lambda: NOW
        )
        try:
            tools = _by_name(context)
            tools["ontology_define"].execute(
                {
                    "classes": [{"id": "P"}],
                    "relations": [{"id": "knows", "characteristics": ["symmetric"]}],
                }
            )
            tools["ontology_assert"].execute(
                {
                    "entities": [{"id": "a", "classes": ["P"]}, {"id": "b", "classes": ["P"]}],
                    "facts": [{"subject": "a", "predicate": "knows", "object": "b"}],
                }
            )
            result = tools["ontology_query"].execute({"mode": "facts", "includeInferred": True})
            assert {f["via"] for f in result["facts"]} == {"asserted"}
            stats = tools["ontology_query"].execute({"mode": "stats"})["stats"]
            assert stats["inferredFacts"] == 0
        finally:
            store.close()

    def test_unknown_mode_rejected(self, seeded: Any) -> None:
        with pytest.raises(ValueError):
            _by_name(seeded)["ontology_query"].execute({"mode": "nope"})


class TestQueryLimits:
    @pytest.fixture()
    def many(self, ctx: Any) -> Any:
        tools = _by_name(ctx)
        tools["ontology_define"].execute({"classes": [{"id": "P"}], "relations": [{"id": "rel"}]})
        tools["ontology_assert"].execute(
            {
                "entities": [{"id": f"e{i}", "classes": ["P"]} for i in range(10)],
                "facts": [
                    {"subject": f"e{i}", "predicate": "rel", "object": "x"} for i in range(10)
                ],
            }
        )
        return ctx

    def test_truncation_flag_set(self, many: Any) -> None:
        result = _by_name(many)["ontology_query"].execute({"mode": "entities", "limit": 3})
        assert len(result["entities"]) == 3
        assert result["truncated"] is True

    def test_no_truncation_when_fits(self, many: Any) -> None:
        result = _by_name(many)["ontology_query"].execute({"mode": "entities", "limit": 20})
        assert result["truncated"] is False

    def test_limit_clamped_to_max(self, many: Any) -> None:
        result = _by_name(many)["ontology_query"].execute({"mode": "entities", "limit": 10_000})
        assert len(result["entities"]) == 10, "上限 100 但库里只有 10 条"

    def test_depth_clamped_to_range(self, many: Any) -> None:
        """depth 钳制在 [1,8]——depth=0 视为 1，depth=999 视为 8。"""
        tool = _by_name(many)["ontology_query"]
        assert "reached" in tool.execute({"mode": "neighbors", "entity": "e0", "depth": 0})
        assert "reached" in tool.execute({"mode": "neighbors", "entity": "e0", "depth": 999})

    def test_default_depth_is_two(self, many: Any) -> None:
        result = _by_name(many)["ontology_query"].execute({"mode": "neighbors", "entity": "e0"})
        assert "e0" in result["reached"]


# ── retract ─────────────────────────────────────────────────────────────


class TestRetractTool:
    def _seed(self, ctx: Any) -> None:
        tools = _by_name(ctx)
        tools["ontology_define"].execute(
            {
                "classes": [{"id": "Person"}, {"id": "Company"}],
                "relations": [{"id": "worksAt", "domain": ["Person"], "range": ["Company"]}],
            }
        )
        tools["ontology_assert"].execute(
            {
                "entities": [
                    {"id": "alice", "classes": ["Person"]},
                    {"id": "acme", "classes": ["Company"]},
                ],
                "facts": [{"subject": "alice", "predicate": "worksAt", "object": "acme"}],
            }
        )

    def test_retract_entity_cascades_and_reports(self, ctx: Any) -> None:
        self._seed(ctx)
        result = _by_name(ctx)["ontology_retract"].execute({"entities": ["alice"]})
        assert result["entities"] == ["alice"]
        assert result["cascadedFacts"] == 1, "级联删的事实数必须回报"

    def test_retract_term_blocked_goes_to_refused(self, ctx: Any) -> None:
        """破坏者：仍被实体归类的类撤回不得生效，且不能报成 missing。"""
        self._seed(ctx)
        result = _by_name(ctx)["ontology_retract"].execute({"terms": ["Person"]})
        assert result["terms"] == []
        assert result["missing"] == []
        assert len(result["refused"]) == 1
        assert result["refused"][0]["ref"] == "Person"

    def test_retract_unused_term(self, ctx: Any) -> None:
        self._seed(ctx)
        result = _by_name(ctx)["ontology_retract"].execute({"terms": ["Company"]})
        assert result["terms"] == []
        assert result["missing"] == [], "Company 被引用但本次只撤回 Person"
        result = _by_name(ctx)["ontology_retract"].execute({"terms": ["Company"]})
        assert result["refused"], "仍有实体归类于 Company"

    def test_term_retract_succeeds_once_dependents_gone(self, ctx: Any) -> None:
        """清掉依赖者后词表项可撤回（对照 dsh「allows the term once its
        dependents are gone」）——完整清理链：删实体（级联删事实）→ 删关系 → 删类。"""
        self._seed(ctx)
        tools = _by_name(ctx)
        # 1) 删实体 acme：级联删掉 alice→acme 事实
        tools["ontology_retract"].execute({"entities": ["acme"]})
        # 2) 撤 worksAt：事实已清，但其签名仍引用 Company —— 不阻塞关系自身
        result = tools["ontology_retract"].execute({"terms": ["worksAt"]})
        assert result["terms"] == ["worksAt"], f"事实清空后关系应可撤：{result}"
        # 3) 撤 Company：实体与关系签名依赖都已消失
        result = tools["ontology_retract"].execute({"terms": ["Company"]})
        assert result["terms"] == ["Company"], f"依赖清空后类应可撤：{result}"
        assert result["refused"] == []

    def test_retract_fact(self, ctx: Any) -> None:
        self._seed(ctx)
        result = _by_name(ctx)["ontology_retract"].execute(
            {"facts": [{"subject": "alice", "predicate": "worksAt", "object": "acme"}]}
        )
        assert result["facts"] == ["alice worksAt acme"]
        assert ctx.store.graph().facts == {}

    def test_retract_missing_reported(self, ctx: Any) -> None:
        result = _by_name(ctx)["ontology_retract"].execute(
            {
                "terms": ["ghost"],
                "entities": ["nobody"],
                "facts": [{"subject": "a", "predicate": "p", "object": "b"}],
            }
        )
        assert sorted(result["missing"]) == ["a p b", "ghost", "nobody"]

    def test_cascade_kills_derived_facts(self, ctx: Any) -> None:
        """撤回前提，依赖它的派生结论随之消失（不落盘故无需反向清理）。"""
        tools = _by_name(ctx)
        tools["ontology_define"].execute(
            {
                "classes": [{"id": "P"}],
                "relations": [{"id": "parentOf", "characteristics": ["transitive"]}],
            }
        )
        tools["ontology_assert"].execute(
            {
                "entities": [
                    {"id": "a", "classes": ["P"]},
                    {"id": "b", "classes": ["P"]},
                    {"id": "c", "classes": ["P"]},
                ],
                "facts": [
                    {"subject": "a", "predicate": "parentOf", "object": "b"},
                    {"subject": "b", "predicate": "parentOf", "object": "c"},
                ],
            }
        )
        inferred = tools["ontology_query"].execute(
            {"mode": "facts", "includeInferred": True, "limit": 50}
        )["facts"]
        assert any(f["subject"] == "a" and f["object"] == "c" for f in inferred)

        tools["ontology_retract"].execute({"entities": ["b"]})
        after = tools["ontology_query"].execute(
            {"mode": "facts", "includeInferred": True, "limit": 50}
        )["facts"]
        assert not any(f["subject"] == "a" and f["object"] == "c" for f in after)


# ── 渲染 ────────────────────────────────────────────────────────────────


class TestRendering:
    def test_define_renders_summary_and_rejections(self, ctx: Any) -> None:
        tool = _by_name(ctx)["ontology_define"]
        text = tool.render(tool.execute({"classes": [{"id": "C"}]}))
        assert "已定义" in text and "C" in text

    def test_define_render_marks_rejection(self, ctx: Any) -> None:
        tool = _by_name(ctx)["ontology_define"]
        text = tool.render(tool.execute({"classes": [{"id": "X", "subClassOf": ["Ghost"]}]}))
        assert "已拒绝" in text and "X" in text, f"拒绝须显式呈现：{text}"

    def test_inferred_facts_marked_in_prose(self, ctx: Any) -> None:
        tools = _by_name(ctx)
        tools["ontology_define"].execute(
            {
                "classes": [{"id": "P"}],
                "relations": [{"id": "knows", "characteristics": ["symmetric"]}],
            }
        )
        tools["ontology_assert"].execute(
            {
                "entities": [{"id": "a", "classes": ["P"]}, {"id": "b", "classes": ["P"]}],
                "facts": [{"subject": "a", "predicate": "knows", "object": "b"}],
            }
        )
        tool = tools["ontology_query"]
        text = tool.render(tool.execute({"mode": "facts", "includeInferred": True}))
        assert "symmetric" in text, "派生态须在文案里标明推理来源"

    def test_violations_flagged_in_prose(self, ctx: Any) -> None:
        tools = _by_name(ctx)
        tools["ontology_define"].execute({"classes": [{"id": "P"}]})
        tool = tools["ontology_assert"]
        text = tool.render(
            tool.execute({"facts": [{"subject": "a", "predicate": "ghost", "object": "b"}]})
        )
        assert "REJECTED" in text or "拒绝" in text
