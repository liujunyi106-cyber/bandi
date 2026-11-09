"""契约：本体词表提示词注入 + 记忆侧推理接线。

复刻 参考实现 `src/prompt.ts`（57 行）。

行为不变式：

**词表注入**
 1. **只注词汇，不注实例**——TBox 小、慢变、便宜；实例数据大、快变，
 属于 `ontology_query` 的职责
 2. 词表摘要**≤ 60 词**（超限截断并留省略提示）
 3. 顺序稳定（先类后关系，各按 id）→ 未变的图产生未变的提示词，缓存友好
 4. **空图给引导语而非空列表**——列出「什么都没有」教不会模型任何东西
 5. 非空图给出计数（类/关系/实体/事实）+ 词汇清单 + 读/扩展指引

**记忆侧推理接线（D5）**
 6. `tbox.entailed_edges` 从零生产调用变为有生产调用点——
 记忆三元组按 symmetric/transitive/inverse 推导，派生不落盘"""

from __future__ import annotations

from pathlib import Path

from bandi.ontology.prompt import MAX_TBOX_TERMS, render_ontology_section
from bandi.ontology.types import ClassTerm, Entity, Fact, GraphView, RelationTerm

NOW = "2026-10-13T00:00:00Z"


def _g(terms: dict | None = None, entities: dict | None = None, facts: dict | None = None):
    return GraphView(terms=terms or {}, entities=entities or {}, facts=facts or {})


class TestEmptyGraphSection:
    def test_empty_graph_gets_guidance_not_empty_list(self) -> None:
        """空图必须给引导语——列出「什么都没有」教不会模型任何东西。"""
        text = render_ontology_section(_g(), 60)
        assert "本体" in text or "ontology" in text.lower()
        assert len(text.splitlines()) > 3, "空图也要有实质说明"

    def test_guidance_names_the_four_tools(self) -> None:
        text = render_ontology_section(_g(), 60)
        assert "ontology_define" in text
        assert "ontology_assert" in text

    def test_guidance_warns_against_scratchpad_use(self) -> None:
        text = render_ontology_section(_g(), 60)
        assert "草稿" in text or "临时" in text, "须劝阻把本体当草稿纸"

    def test_empty_graph_omits_counts(self) -> None:
        assert "0 classes" not in render_ontology_section(_g(), 60)


class TestPopulatedGraphSection:
    def _graph(self) -> GraphView:
        return _g(
            terms={
                "Person": ClassTerm(
                    kind="class", id="Person", sub_class_of=(), created_at=NOW, updated_at=NOW
                ),
                "Company": ClassTerm(
                    kind="class", id="Company", sub_class_of=(), created_at=NOW, updated_at=NOW
                ),
                "worksAt": RelationTerm(
                    kind="relation",
                    id="worksAt",
                    domain=("Person",),
                    range=("Company",),
                    characteristics=("functional",),
                    created_at=NOW,
                    updated_at=NOW,
                ),
            },
            entities={
                "alice": Entity(id="alice", classes=("Person",), created_at=NOW, updated_at=NOW)
            },
            facts={
                "alice|worksAt|acme": Fact(
                    subject="alice",
                    predicate="worksAt",
                    object="acme",
                    object_kind="entity",
                    violations=(),
                    created_at=NOW,
                )
            },
        )

    def test_counts_present(self) -> None:
        text = render_ontology_section(self._graph(), 60)
        assert "2" in text and "worksAt" in text

    def test_vocabulary_rendered_as_bullets(self) -> None:
        text = render_ontology_section(self._graph(), 60)
        assert "class Person" in text
        assert "rel worksAt" in text

    def test_instance_data_not_injected(self) -> None:
        """只注词汇不注实例——alice 是实例数据，属 ontology_query 的职责。"""
        text = render_ontology_section(self._graph(), 60)
        assert "alice" not in text

    def test_read_and_extend_guidance_present(self) -> None:
        text = render_ontology_section(self._graph(), 60)
        assert "ontology_query" in text, "须告诉模型怎么读"
        assert "ontology_define" in text, "须告诉模型怎么扩展"

    def test_order_stable_across_calls(self) -> None:
        graph = self._graph()
        assert render_ontology_section(graph, 60) == render_ontology_section(graph, 60)

    def test_term_cap_is_sixty(self) -> None:
        """计划 -6：词表摘要 ≤60 词。"""
        assert MAX_TBOX_TERMS == 60

    def test_overflow_produces_elision_note(self) -> None:
        terms = {
            f"C{i}": ClassTerm(
                kind="class", id=f"C{i}", sub_class_of=(), created_at=NOW, updated_at=NOW
            )
            for i in range(80)
        }
        text = render_ontology_section(_g(terms=terms), MAX_TBOX_TERMS)
        assert "class C79" not in text, "超限须截断"
        assert "词表项" in text or "more terms" in text.lower(), "截断须留提示"


class TestTboxWiring:
    """D5：记忆侧推理引擎接线——`entailed_edges` 当前零生产调用。"""

    def test_memory_inference_transitive_closure(self) -> None:
        """记忆三元组按传递特征推导，派生不落盘。

        `entailed_edges` 按 `relation_for()` 查 `RELATION_TERMS`（键为谓词 id），
        故这里取表内已有的传递谓词，不自造。
        """
        from bandi.memory.tbox import RELATION_TERMS, entailed_edges, relation_for

        transitive = [rid for rid in RELATION_TERMS if relation_for(rid).transitive]
        assert transitive, "tbox 须至少有一个传递谓词可供接线验证"
        predicate = transitive[0]
        edges = entailed_edges([("a", predicate, "b"), ("b", predicate, "c")])
        assert ("a", predicate, "c") in edges, "传递闭包须生效"

    def test_derived_edges_do_not_mutate_input(self) -> None:
        """推论结果是新增项，不改传入的边集合（派生不落盘）。"""
        from bandi.memory.tbox import RELATION_TERMS, entailed_edges, relation_for

        symmetric = [rid for rid in RELATION_TERMS if relation_for(rid).symmetric]
        assert symmetric, "tbox 须至少有一个对称谓词"
        predicate = symmetric[0]
        original = [("a", predicate, "b")]
        derived = entailed_edges(list(original))
        assert derived, "对称关系须推出镜像"
        assert original == [("a", predicate, "b")], "传入的边不得被就地修改"

    def test_entailment_reaches_fixpoint(self) -> None:
        """把已推出的边再喂回去不得再产生新结论——已到不动点。"""
        from bandi.memory.tbox import RELATION_TERMS, entailed_edges, relation_for

        transitive = [rid for rid in RELATION_TERMS if relation_for(rid).transitive]
        predicate = transitive[0]
        base = [("a", predicate, "b"), ("b", predicate, "c")]
        first = entailed_edges(list(base))
        second = entailed_edges([*base, *first])
        assert second == [], "已达不动点后不应再推出新边"


class TestMemoryTriplesEntailment:
    """D5 实质接线：记忆侧三元组按 tbox 特征推导（`entailed_structured`）。"""

    def test_transitive_triples_derive(self) -> None:
        from bandi.memory.structured import entailed_structured
        from bandi.memory.tbox import RELATION_TERMS, relation_for

        predicate = next(rid for rid in RELATION_TERMS if relation_for(rid).transitive)
        given = [
            {
                "kind": "fact",
                "key": predicate,
                "subject": "a",
                "predicate": predicate,
                "object": "b",
            },
            {
                "kind": "fact",
                "key": predicate,
                "subject": "b",
                "predicate": predicate,
                "object": "c",
            },
        ]
        derived = entailed_structured(given)
        triples = {(d["subject"], d["predicate"], d["object"]) for d in derived}
        assert ("a", predicate, "c") in triples

    def test_derived_marked_inferred(self) -> None:
        """派生态必须带标记——否则与断言事实无法区分，巩固会把它当新事实再写一遍。"""
        from bandi.memory.structured import entailed_structured
        from bandi.memory.tbox import RELATION_TERMS, relation_for

        predicate = next(rid for rid in RELATION_TERMS if relation_for(rid).transitive)
        given = [
            {
                "kind": "fact",
                "key": predicate,
                "subject": "a",
                "predicate": predicate,
                "object": "b",
            },
            {
                "kind": "fact",
                "key": predicate,
                "subject": "b",
                "predicate": predicate,
                "object": "c",
            },
        ]
        derived = entailed_structured(given)
        assert all(d.get("inferred") is True for d in derived), "派生态须标记 inferred"
        assert all(d.get("derived_via") for d in derived), "须标明推理来源"

    def test_asserted_triples_untouched(self) -> None:
        """输入的断言事实不得被就地改写（派生不落盘）。"""
        from bandi.memory.structured import entailed_structured
        from bandi.memory.tbox import RELATION_TERMS, relation_for

        predicate = next(rid for rid in RELATION_TERMS if relation_for(rid).transitive)
        given = [
            {
                "kind": "fact",
                "key": predicate,
                "subject": "a",
                "predicate": predicate,
                "object": "b",
            }
        ]
        snapshot = [dict(item) for item in given]
        entailed_structured(given)
        assert given == snapshot, "传入的断言事实不得被修改"

    def test_non_triple_entries_ignored(self) -> None:
        """非三元组形态（preference / 只有 key:value）不得参与推导。"""
        from bandi.memory.structured import entailed_structured

        assert entailed_structured([{"kind": "preference", "key": "preference"}]) == []
        assert entailed_structured([]) == []

    def test_unknown_predicate_ignored(self) -> None:
        """谓词不在 tbox 白名单内——无从判断特征，跳过而非报错。"""
        from bandi.memory.structured import entailed_structured

        given = [{"kind": "fact", "key": "zzz", "subject": "a", "predicate": "zzz", "object": "b"}]
        assert entailed_structured(given) == []

    def test_no_production_imports_broken(self) -> None:
        """本体模块不得反向导入记忆侧模块。"""
        import bandi.ontology.engine as engine
        import bandi.ontology.tools as tools_mod
        import bandi.ontology.triple_store as triple

        for module in (engine, tools_mod, triple):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "from bandi.memory" not in source, (
                f"{module.__name__} 反向导入了记忆模块，违反两套体系边界"
            )
