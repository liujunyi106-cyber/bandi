"""契约：本体推理内核（对照表 -2 / -2,3,4）。

复刻 `src/ontology.ts`（749 行全读）。行为不变式

**标识与闭包**
  1. `check_id` 管字符语法 + 长度上限（≤128）——正则不表达长度
  2. `class_ancestors` 含自身且环安全；`class_descendants` 不动点
  3. `entity_classes` = 直接类 ∪ 全部祖先（继承）

**四层校验**
  4. class：kind 冲突 / 未知父类 / `subClassOf` 成环
  5. relation：kind 冲突 / domain·range 未知类 / **literal 关系不得
     symmetric/transitive/inverse**（派生命题的主语会是字面量）/ inverse 须为关系
  6. entity：id 语法 + 类必须已定义
  7. fact：未知谓词 → 未知主语 → domain 匹配 → objectKind/range 匹配 →
     **functional 冲突**（同一主语不得指向两个不同 object）

**推理三规则**
  8. inverse：声明 `inverseOf` 即双向推导
  9. symmetric：自反无意义时跳过自环
  10. transitive：链式合成，且**不产生自环**
  11. **不动点**（≤32 轮）——规则可复合：传递关系的逆关系本身也传递闭合
  12. **派生态不落盘**——`entail` 只返回，不写库；派生物不得覆盖已断言事实
  13. 每条派生物带 `via` 标记，可解释

**查询六模式**
  14. `query_entities` 类匹配双向闭包（问 Service 返回其子类 HttpService 的成员）
  15. `query_facts` 三位通配 + `includeInferred`；断言在前
  16. `neighbors` 有向双向按半径扩散
  17. `find_path` 边视为无向找最短链
  18. `stats_of` / `render_tbox`（≤60 词、顺序稳定 → prompt 缓存友好）

`src/ontology.ts:113-749`。
"""

from __future__ import annotations

import pytest

from bandi.ontology.engine import (
    MAX_ENTAILMENT_ROUNDS,
    all_facts,
    check_id,
    class_ancestors,
    class_descendants,
    entail,
    entity_classes,
    find_path,
    neighbors,
    query_entities,
    query_facts,
    render_tbox,
    stats_of,
    validate_class_input,
    validate_entity_input,
    validate_fact_input,
    validate_relation_input,
)
from bandi.ontology.types import (
    ClassTerm,
    Entity,
    Fact,
    GraphView,
    RelationTerm,
    Violation,
)

NOW = "2026-10-13T00:00:00Z"


def _g(
    terms: dict | None = None, entities: dict | None = None, facts: dict | None = None
) -> GraphView:
    return GraphView(terms=terms or {}, entities=entities or {}, facts=facts or {})


def _cls(cid: str, parents: tuple[str, ...] = ()) -> ClassTerm:
    return ClassTerm(kind="class", id=cid, sub_class_of=parents, created_at=NOW, updated_at=NOW)


def _rel(rid: str, **kw) -> RelationTerm:
    return RelationTerm(
        kind="relation",
        id=rid,
        domain=kw.get("domain", ()),
        range_kind=kw.get("range_kind", "entity"),
        range=kw.get("range", ()),
        characteristics=kw.get("characteristics", ()),
        inverse_of=kw.get("inverse_of"),
        created_at=NOW,
        updated_at=NOW,
    )


def _ent(eid: str, classes: tuple[str, ...] = ()) -> Entity:
    return Entity(id=eid, classes=classes, created_at=NOW, updated_at=NOW)


def _f(s: str, p: str, o: str, kind: str = "entity") -> Fact:

    return Fact(subject=s, predicate=p, object=o, object_kind=kind, violations=(), created_at=NOW)


def _codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


# ── 标识 ────────────────────────────────────────────────────────────────


class TestCheckId:
    @pytest.mark.parametrize("value", ["alice", "_x", "schema:Person", "repo.Module", "a-b.c_d:1"])
    def test_accepts_legal(self, value: str) -> None:
        assert check_id(value, "class") is None

    @pytest.mark.parametrize(
        ("value", "code"),
        [
            ("", "invalid-id"),
            ("1abc", "invalid-id"),
            ("-abc", "invalid-id"),
            ("a b", "invalid-id"),
            ("a|b", "invalid-id"),
            ("A" * 129, "invalid-id"),
        ],
    )
    def test_rejects_illegal(self, value: str, code: str) -> None:
        violation = check_id(value, "class")
        assert violation is not None
        assert violation.code == code

    def test_length_limit_and_message_format(self) -> None:
        """正则在 fullmatch 下不校验长度——上限必须显式判；文案点名角色与出错值。"""
        assert check_id("A" * 128, "class") is None
        assert check_id("A" * 129, "class") is not None
        violation = check_id("1bad", "relation")
        assert "relation" in violation.message and "1bad" in violation.message


# ── 类闭包 ──────────────────────────────────────────────────────────────


class TestSubsumption:
    def test_ancestors_include_self_and_cycle_safe(self) -> None:
        """环安全：visited 集兼作结果集，故成环也终止而非挂死 agent loop。"""
        terms = {"C": _cls("C"), "B": _cls("B", ("C",)), "A": _cls("A", ("B",))}
        assert class_ancestors(terms, "A") == {"A", "B", "C"}
        terms = {"A": _cls("A", ("B",)), "B": _cls("B", ("A",))}
        assert class_ancestors(terms, "A") == {"A", "B"}

    def test_descendants_fixpoint_and_leaf(self) -> None:
        terms = {
            "A": _cls("A"),
            "B": _cls("B", ("A",)),
            "C": _cls("C", ("B",)),
            "X": _cls("X"),
        }
        assert class_descendants(terms, "A") == {"A", "B", "C"}
        assert class_descendants(terms, "X") == {"X"}
        assert class_descendants({"C": _cls("C")}, "C") == {"C"}

    def test_entity_classes_closed_upward_and_unknown_empty(self) -> None:
        terms = {"A": _cls("A"), "B": _cls("B", ("A",)), "C": _cls("C", ("B",))}
        graph = _g(terms=terms, entities={"e": _ent("e", ("C",))})
        assert entity_classes(graph, "e") == {"A", "B", "C"}
        assert entity_classes(_g(), "nope") == set()


# ── 四层校验 ────────────────────────────────────────────────────────────


class TestValidateClass:
    def test_accepts_valid(self) -> None:
        graph = _g(terms={"A": _cls("A")})
        assert validate_class_input(graph, {"id": "B", "sub_class_of": ["A"]}) == []

    def test_rejects_kind_conflicts(self) -> None:
        graph = _g(terms={"X": _rel("X")})
        assert "kind-conflict" in _codes(validate_class_input(graph, {"id": "X"}))
        graph = _g(terms={"R": _rel("R")})
        violations = validate_class_input(graph, {"id": "B", "sub_class_of": ["R"]})
        assert "kind-conflict" in _codes(violations)

    def test_rejects_unknown_or_self_parent(self) -> None:
        """自指父类。B 尚未定义时只报「未知类」——存在性检查先于成环检查
        （同 dsh `ontology.ts:217` 的 `continue`），定义后才升级为成环。"""
        assert "unknown-class" in _codes(
            validate_class_input(_g(), {"id": "B", "sub_class_of": ["Nope"]})
        )
        assert "unknown-class" in _codes(
            validate_class_input(_g(), {"id": "B", "sub_class_of": ["B"]})
        )
        # 已定义的自指才走成环分支
        terms = {"B": _cls("B")}
        assert "subclass-cycle" in _codes(
            validate_class_input(_g(terms=terms), {"id": "B", "sub_class_of": ["B"]})
        )

    def test_rejects_cycle_with_existing_ancestor(self) -> None:
        """破坏者：让 A 继承 B 的后代会成环——「是 X」将永不可证伪。"""
        terms = {"A": _cls("A"), "B": _cls("B", ("A",))}
        assert "subclass-cycle" in _codes(
            validate_class_input(_g(terms=terms), {"id": "A", "sub_class_of": ["B"]})
        )


class TestValidateRelation:
    def test_accepts_valid_and_self_inverse_forward_reference(self) -> None:
        """自逆是唯一值得放行的前向引用——否则一对互逆关系要分两次定义。"""
        graph = _g(terms={"Person": _cls("Person"), "Company": _cls("Company")})
        violations = validate_relation_input(
            graph,
            {
                "id": "worksAt",
                "domain": ["Person"],
                "range": ["Company"],
                "characteristics": ["functional"],
            },
        )
        assert violations == []
        assert validate_relation_input(_g(), {"id": "marriedTo", "inverse_of": "marriedTo"}) == []

    def test_rejects_kind_conflicts(self) -> None:
        graph = _g(terms={"X": _cls("X")})
        assert "kind-conflict" in _codes(validate_relation_input(graph, {"id": "X"}))
        graph = _g(terms={"C": _cls("C")})
        violations = validate_relation_input(graph, {"id": "r", "inverse_of": "C"})
        assert "kind-conflict" in _codes(violations)

    @pytest.mark.parametrize("role", ["domain", "range"])
    def test_rejects_unknown_class(self, role: str) -> None:
        violations = validate_relation_input(_g(), {"id": "r", role: ["Nope"]})
        assert "unknown-class" in _codes(violations)

    def test_literal_relation_rejects_class_range_and_inverse(self) -> None:
        violations = validate_relation_input(
            _g(), {"id": "age", "range_kind": "literal", "range": ["Person"]}
        )
        assert "literal-range" in _codes(violations)
        violations = validate_relation_input(
            _g(), {"id": "age", "range_kind": "literal", "inverse_of": "agedBy"}
        )
        assert "literal-inverse" in _codes(violations)
        graph = _g(terms={"agedBy": _rel("agedBy", range_kind="literal")})
        violations = validate_relation_input(graph, {"id": "r", "inverse_of": "agedBy"})
        assert "literal-inverse" in _codes(violations)

    @pytest.mark.parametrize("ch", ["symmetric", "transitive"])
    def test_literal_rejects_characteristics(self, ch: str) -> None:
        """字面量关系的派生命题会把字面量放在主语位置——不成立。"""
        violations = validate_relation_input(
            _g(), {"id": "age", "range_kind": "literal", "characteristics": [ch]}
        )
        assert "literal-characteristic" in _codes(violations)

    def test_rejects_unknown_inverse(self) -> None:
        assert "unknown-relation" in _codes(
            validate_relation_input(_g(), {"id": "a", "inverse_of": "b"})
        )


class TestValidateEntity:
    def test_accepts_valid(self) -> None:
        graph = _g(terms={"Person": _cls("Person")}, entities={})
        assert validate_entity_input(graph, {"id": "alice", "classes": ["Person"]}) == []

    def test_rejects_bad_id_and_unknown_class(self) -> None:
        assert "invalid-id" in _codes(validate_entity_input(_g(), {"id": "1bad"}))
        assert "unknown-class" in _codes(
            validate_entity_input(_g(), {"id": "e", "classes": ["Nope"]})
        )


class TestValidateFact:
    def _base(self) -> GraphView:
        return _g(
            terms={
                "Person": _cls("Person"),
                "Company": _cls("Company"),
                "worksAt": _rel("worksAt", domain=("Person",), range=("Company",)),
            },
            entities={"alice": _ent("alice", ("Person",)), "acme": _ent("acme", ("Company",))},
        )

    def test_accepts_valid(self) -> None:
        assert (
            validate_fact_input(
                self._base(), {"subject": "alice", "predicate": "worksAt", "object": "acme"}
            )
            == []
        )

    def test_rejects_unknown_predicate_and_subject(self) -> None:
        """谓词未定义后其余规则都读不到关系——必须提前返回，不叠加噪音违规。

        面向模型可修复：文案须点名出错值与被破坏约束。
        """
        violations = validate_fact_input(
            self._base(), {"subject": "alice", "predicate": "nope", "object": "acme"}
        )
        assert _codes(violations) == {"unknown-relation"}
        violations = validate_fact_input(
            self._base(), {"subject": "ghost", "predicate": "worksAt", "object": "acme"}
        )
        assert "unknown-entity" in _codes(violations)
        assert any("ghost" in v.message for v in violations)

    def test_domain_mismatch_and_subsumption_match(self) -> None:
        """子类成员也满足父类域约束（继承生效）。"""
        graph = _g(
            terms={"Person": _cls("Person"), "worksAt": _rel("worksAt", domain=("Person",))},
            entities={"acme": _ent("acme", ("Company",))},
        )
        violations = validate_fact_input(
            graph, {"subject": "acme", "predicate": "worksAt", "object": "acme"}
        )
        assert "domain-mismatch" in _codes(violations)
        graph = _g(
            terms={
                "Person": _cls("Person"),
                "Engineer": _cls("Engineer", ("Person",)),
                "worksAt": _rel("worksAt", domain=("Person",)),
            },
            entities={"bob": _ent("bob", ("Engineer",))},
        )
        assert (
            validate_fact_input(graph, {"subject": "bob", "predicate": "worksAt", "object": "bob"})
            == []
        )

    def test_rejects_unknown_object_and_range_mismatch(self) -> None:
        violations = validate_fact_input(
            self._base(), {"subject": "alice", "predicate": "worksAt", "object": "ghost"}
        )
        assert "unknown-entity" in _codes(violations)
        graph = _g(
            terms={
                "Person": _cls("Person"),
                "Company": _cls("Company"),
                "worksAt": _rel("worksAt", domain=("Person",), range=("Company",)),
            },
            entities={"alice": _ent("alice", ("Person",)), "x": _ent("x", ("Person",))},
        )
        violations = validate_fact_input(
            graph, {"subject": "alice", "predicate": "worksAt", "object": "x"}
        )
        assert "range-mismatch" in _codes(violations)

    def test_literal_range_accepts_value_and_rejects_empty(self) -> None:
        graph = _g(
            terms={"Person": _cls("Person"), "age": _rel("age", range_kind="literal")},
            entities={"alice": _ent("alice", ("Person",))},
        )
        assert (
            validate_fact_input(graph, {"subject": "alice", "predicate": "age", "object": "30"})
            == []
        )
        assert "empty-literal" in _codes(
            validate_fact_input(graph, {"subject": "alice", "predicate": "age", "object": ""})
        )

    def test_functional_conflict_and_reassert(self) -> None:
        """破坏者：functional 关系不得让同一主语指向两个不同宾语。"""
        graph = _g(
            terms={
                "Person": _cls("Person"),
                "worksAt": _rel("worksAt", domain=("Person",), characteristics=("functional",)),
            },
            entities={"alice": _ent("alice", ("Person",)), "a1": _ent("a1"), "a2": _ent("a2")},
            facts={"alice|worksAt|a1": _f("alice", "worksAt", "a1")},
        )
        violations = validate_fact_input(
            graph, {"subject": "alice", "predicate": "worksAt", "object": "a2"}
        )
        assert "functional-conflict" in _codes(violations)
        graph = _g(
            terms={
                "Person": _cls("Person"),
                "worksAt": _rel("worksAt", domain=("Person",), characteristics=("functional",)),
            },
            entities={"alice": _ent("alice", ("Person",)), "a1": _ent("a1")},
            facts={"alice|worksAt|a1": _f("alice", "worksAt", "a1")},
        )
        assert (
            validate_fact_input(graph, {"subject": "alice", "predicate": "worksAt", "object": "a1"})
            == []
        )


# ── 推理三规则 ──────────────────────────────────────────────────────────


class TestEntailInverse:
    def test_derives_reverse_and_no_self_loop(self) -> None:
        """方向：`worksAt` 声明 `inverseOf: employedBy` → 推出 acme employedBy alice。

        `inverseOf` 挂在**持有该声明的关系**上（同 dsh `ontology.ts:502`），
        不是挂在被推导出的那一端。

        自逆关系不产生派生物——`b marriedTo a` 就是同一条断言的重复。
        dsh `ontology.ts:502` 以 `inverseOf !== relation.id` 排除自逆，
        故这里推不出任何新事实。
        """
        graph = _g(
            terms={
                "worksAt": _rel("worksAt", inverse_of="employedBy"),
                "employedBy": _rel("employedBy"),
            },
            facts={"alice|worksAt|acme": _f("alice", "worksAt", "acme")},
        )
        derived = {(d.subject, d.predicate, d.object, d.via) for d in entail(graph)}
        assert ("acme", "employedBy", "alice", "inverse") in derived
        graph = _g(
            terms={"marriedTo": _rel("marriedTo", inverse_of="marriedTo")},
            facts={"a|marriedTo|b": _f("a", "marriedTo", "b")},
        )
        assert entail(graph) == [], "自逆不得推导出镜像三元组"

    def test_literal_relation_not_inferred(self) -> None:
        graph = _g(
            terms={"age": _rel("age", range_kind="literal")},
            facts={"alice|age|30": _f("alice", "age", "30", "literal")},
        )
        assert entail(graph) == []


class TestEntailSymmetric:
    def test_derives_mirror_without_self_loop(self) -> None:
        graph = _g(
            terms={"knows": _rel("knows", characteristics=("symmetric",))},
            facts={"a|knows|b": _f("a", "knows", "b")},
        )
        derived = {(d.subject, d.object, d.via) for d in entail(graph)}
        assert ("b", "a", "symmetric") in derived
        assert not any(d.subject == d.object for d in entail(graph))


class TestEntailTransitive:
    def test_derives_chain_without_round_trip_self_loop(self) -> None:
        graph = _g(
            terms={"subOf": _rel("subOf", characteristics=("transitive",))},
            facts={
                "a|subOf|b": _f("a", "subOf", "b"),
                "b|subOf|c": _f("b", "subOf", "c"),
            },
        )
        derived = {(d.subject, d.object, d.via) for d in entail(graph)}
        assert ("a", "c", "transitive") in derived
        graph = _g(
            terms={"subOf": _rel("subOf", characteristics=("transitive",))},
            facts={
                "a|subOf|b": _f("a", "subOf", "b"),
                "b|subOf|a": _f("b", "subOf", "a"),
            },
        )
        derived = {(d.subject, d.object) for d in entail(graph)}
        assert not any(s == o for s, o in derived), "绕一圈回来不得推出 a subOf a"

    def test_rules_compose_to_fixpoint(self) -> None:
        """破坏者：单轮推导不够——传递 + 逆必须复合到不动点。

        a parentOf b、b parentOf c ⇒ a parentOf c（传递）；
        再对 c parentOf… 段取逆可继续扩展。关键断言：传递推导出的
        `a parentOf c` 本身也参与下一轮，故 `childOf` 方向能拿到 `c childOf a`。
        """
        graph = _g(
            terms={
                "parentOf": _rel("parentOf", characteristics=("transitive",), inverse_of="childOf"),
                "childOf": _rel("childOf"),
            },
            facts={
                "a|parentOf|b": _f("a", "parentOf", "b"),
                "b|parentOf|c": _f("b", "parentOf", "c"),
            },
        )
        derived = {(d.subject, d.predicate, d.object, d.via) for d in entail(graph)}
        assert ("a", "parentOf", "c", "transitive") in derived
        assert ("c", "childOf", "a", "inverse") in derived

    def test_terminates_on_cyclic_relation_and_rounds_bounded(self) -> None:
        """破坏者：a→b→c→a 三元环上的传递关系——不动点必须终止且不产生自环。

        （对照 dsh `ontology.spec.ts`「terminates on a cyclic transitive
        relation」，补 2 环往返之外的 3 环场景。）不动点上限：成环图不会无限迭代。
        """
        graph = _g(
            terms={"subOf": _rel("subOf", characteristics=("transitive",))},
            facts={
                "a|subOf|b": _f("a", "subOf", "b"),
                "b|subOf|c": _f("b", "subOf", "c"),
                "c|subOf|a": _f("c", "subOf", "a"),
            },
        )
        derived = entail(graph)  # 终止即通过（成环不挂死）
        assert not any(d.subject == d.object for d in derived), "闭环不得推出自环"
        assert MAX_ENTAILMENT_ROUNDS == 32


class TestEntailSemantics:
    def test_never_overrides_asserted(self) -> None:
        """已断言的事实不得被同名派生覆盖（断言优先）。"""
        graph = _g(
            terms={"knows": _rel("knows", characteristics=("symmetric",))},
            facts={"a|knows|b": _f("a", "knows", "b")},
        )
        derived = entail(graph)
        assert all(d.via != "asserted" for d in derived)

    def test_limit_caps_derived_count(self) -> None:
        graph = _g(
            terms={"knows": _rel("knows", characteristics=("symmetric",))},
            facts={
                f"n{i}|knows|n{j}": _f(f"n{i}", "knows", f"n{j}")
                for i in range(12)
                for j in range(12)
                if i != j
            },
        )
        assert len(entail(graph, limit=10)) <= 10

    def test_all_facts_asserted_first_and_without_inference(self) -> None:
        graph = _g(
            terms={"knows": _rel("knows", characteristics=("symmetric",))},
            facts={"a|knows|b": _f("a", "knows", "b")},
        )
        facts = all_facts(graph, True)
        assert facts[0].via == "asserted"
        graph = _g(facts={"a|knows|b": _f("a", "knows", "b")})
        assert [d.via for d in all_facts(graph, False)] == ["asserted"]


# ── 查询六模式 ──────────────────────────────────────────────────────────


class TestQueryEntities:
    def _graph(self) -> GraphView:
        return _g(
            terms={"Service": _cls("Service"), "HttpService": _cls("HttpService", ("Service",))},
            entities={
                "s1": _ent("s1", ("Service",)),
                "h1": _ent("h1", ("HttpService",)),
                "z1": _ent("z1", ("Other",)),
            },
        )

    def test_class_match_includes_subclass_members(self) -> None:
        ids = {e.id for e in query_entities(self._graph(), {"class_id": "Service"})}
        assert ids == {"s1", "h1"}, "问父类须返回子类成员"

    def test_free_text_search(self) -> None:
        """搜索面是 id + label + comment（类名不参与），同 dsh `ontology.ts:562`。"""
        graph = _g(
            terms={"Service": _cls("Service"), "HttpService": _cls("HttpService", ("Service",))},
            entities={
                "s1": _ent("s1", ("Service",)),
                "h1": Entity(
                    id="h1",
                    classes=("HttpService",),
                    label="网关服务",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                "z1": _ent("z1", ("Other",)),
            },
        )
        assert "h1" in {e.id for e in query_entities(graph, {"search": "网关"})}

    def test_limit_truncates_and_results_ordered(self) -> None:
        assert len(query_entities(self._graph(), {"limit": 1})) == 1
        got = [e.id for e in query_entities(self._graph(), {})]
        assert got == sorted(got)


class TestQueryFacts:
    def _graph(self) -> GraphView:
        return _g(
            terms={"knows": _rel("knows", characteristics=("symmetric",))},
            facts={"a|knows|b": _f("a", "knows", "b"), "c|knows|d": _f("c", "knows", "d")},
        )

    def test_wildcard_positions(self) -> None:
        assert len(query_facts(self._graph(), {})) == 2
        assert len(query_facts(self._graph(), {"subject": "a"})) == 1

    def test_include_inferred_and_asserted_before_inferred(self) -> None:
        assert len(query_facts(self._graph(), {"include_inferred": True})) == 4
        facts = query_facts(self._graph(), {"include_inferred": True})
        assert [d.via for d in facts][:2] == ["asserted", "asserted"]


class TestNeighbors:
    def _graph(self) -> GraphView:
        return _g(
            facts={
                "a|knows|b": _f("a", "knows", "b"),
                "b|knows|c": _f("b", "knows", "c"),
            }
        )

    def test_radius_one_and_two(self) -> None:
        result = neighbors(self._graph(), "a", 1, False)
        assert set(result.reached) == {"a", "b"}
        result = neighbors(self._graph(), "a", 2, False)
        assert set(result.reached) == {"a", "b", "c"}

    def test_bidirectional(self) -> None:
        result = neighbors(self._graph(), "b", 1, False)
        assert set(result.reached) == {"a", "b", "c"}

    def test_depth_below_one_treated_as_one(self) -> None:
        assert set(neighbors(self._graph(), "a", 0, False).reached) == {"a", "b"}


class TestFindPath:
    def _graph(self) -> GraphView:
        return _g(
            facts={
                "a|knows|b": _f("a", "knows", "b"),
                "b|knows|c": _f("b", "knows", "c"),
            }
        )

    def test_finds_chain(self) -> None:
        path = find_path(self._graph(), "a", "c", 3, False)
        assert path is not None and len(path) == 2

    def test_same_node_and_unreachable(self) -> None:
        assert find_path(self._graph(), "a", "a", 3, False) == []
        assert find_path(self._graph(), "a", "zzz", 3, False) is None

    def test_treats_edges_undirected(self) -> None:
        graph = _g(facts={"a|knows|b": _f("a", "knows", "b")})
        assert find_path(graph, "b", "a", 3, False) is not None


class TestStatsAndRender:
    def _graph(self) -> GraphView:
        return _g(
            terms={
                "Person": _cls("Person"),
                "worksAt": _rel("worksAt", domain=("Person",), characteristics=("functional",)),
            },
            entities={"alice": _ent("alice", ("Person",))},
            facts={"alice|worksAt|acme": _f("alice", "worksAt", "acme")},
        )

    def test_stats_counts_all_four(self) -> None:
        stats = stats_of(self._graph())
        assert (stats.classes, stats.relations, stats.entities, stats.facts) == (1, 1, 1, 1)

    def test_render_tbox_lines_and_order_stable(self) -> None:
        """顺序稳定 → 图未变则 prompt 未变 → 缓存友好。"""
        lines = render_tbox(self._graph(), 60)
        assert any(line.startswith("class Person") for line in lines)
        assert any(line.startswith("rel worksAt") for line in lines)
        graph = self._graph()
        assert render_tbox(graph, 60) == render_tbox(graph, 60)

    def test_render_truncates_with_note(self) -> None:
        terms = {f"C{i}": _cls(f"C{i}") for i in range(10)}
        lines = render_tbox(_g(terms=terms), 3)
        assert len([line for line in lines if line.startswith("class ")]) == 3, "只渲染前 3 项"
        assert any("另有" in line and "词表项" in line for line in lines), (
            f"截断须留提示行：{lines}"
        )

    def test_render_relation_details(self) -> None:
        terms = {"a": _rel("a", characteristics=("transitive",), inverse_of="b"), "b": _rel("b")}
        line = next(line for line in render_tbox(_g(terms=terms), 60) if "rel a:" in line)
        assert "transitive" in line and "inverseOf b" in line
        terms = {"age": _rel("age", range_kind="literal")}
        assert any("literal" in line for line in render_tbox(_g(terms=terms), 60))
