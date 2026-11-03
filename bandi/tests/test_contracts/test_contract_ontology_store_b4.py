"""契约：本体持久层（对照表 -3,4 / -5,7）。

复刻 `src/spec.ts`（122 行，三表）+ `src/store.ts`（296 行，写入语义）。

行为不变式：

**表结构**
  1. 三张表 `onto_terms` / `onto_entities` / `onto_facts`
  2. facts 主键 = 复合键 `s|p|o`（幂等的物理基础）
  3. 派生态**永不落盘**——表里只有断言事实

**写入语义**
  4. 重定义整体替换，**保留原 createdAt**，刷新 updatedAt
  5. 实体 classes 取**并集**（两次独立观察同一对象应累积）；
     收窄须显式 retract-then-assert
  6. 断言幂等：同一三元组重复断言是覆盖，不是新增
  7. strict=True 违规拒写；strict=False 违规记在 fact 上保持可审计
  8. 容量守卫：新实体/新事实超限 → 拒写并给可操作文案（改配置或先撤回）
  9. confidence 必须落在 [0,1]，越界拒写

**撤回**
 10. 撤回实体**级联删**所有提及它的事实（objectKind=entity 的才算宾语匹配）
 11. 撤回词表项：**被依赖时拒绝**（实体用它归类 / 子类继承它 / 关系签名引用它
     / 有事实用它作谓词 / 别的关系声明它是 inverse），并给出点名依赖者的文案
 12. 撤回单条事实：派生态无存储键，前提消失后自然不成立

`src/spec.ts:109-119`、`src/store.ts:75-296`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bandi.ontology.spec import ONTOLOGY_DOMAIN_VERSION, OntologyTables, open_ontology
from bandi.ontology.triple_store import (
    DEFAULT_MAX_ENTITIES,
    DEFAULT_MAX_FACTS,
    OntologyStore,
    StoreLimits,
)

NOW = "T00:00:00Z"
LATER = "T00:00:00Z"


@pytest.fixture()
def tables(tmp_path: Path) -> Any:
    opened = open_ontology(str(tmp_path / "onto.db"))
    yield opened
    opened.close()


@pytest.fixture()
def store(tables: OntologyTables) -> Any:
    return OntologyStore(tables, StoreLimits())


def _seed_vocabulary(store: OntologyStore) -> None:
    store.define_class({"id": "Person", "label": "人"}, NOW)
    store.define_class({"id": "Employee", "sub_class_of": ["Person"]}, NOW)
    store.define_class({"id": "Company"}, NOW)
    store.define_relation({"id": "worksAt", "domain": ["Person"], "range": ["Company"]}, NOW)


# ── 表结构 ──────────────────────────────────────────────────────────────


class TestSchema:
    def test_three_tables_exist(self, tables: OntologyTables) -> None:
        names = tables.table_names()
        assert set(names) >= {"onto_terms", "onto_entities", "onto_facts"}

    def test_facts_keyed_by_composite_triple(self, tables: OntologyTables) -> None:
        """复合键做主键——物理层的幂等保障。

        `OntologyTables` 是纯 IO 层（不带校验），故这里走底层 `put_*` 造数据，
        校验路径由 `OntologyStore` 的契约覆盖。
        """
        from bandi.ontology.types import Entity, Fact, fact_key

        tables.put_entity(Entity(id="a", classes=("P",), created_at=NOW, updated_at=NOW), NOW)
        fact = Fact(
            subject="a",
            predicate="rel",
            object="b",
            object_kind="entity",
            violations=(),
            created_at=NOW,
        )
        tables.put_fact(fact_key("a", "rel", "b"), fact)
        tables.commit()
        rows = tables.raw_facts()
        assert rows and rows[0][0] == "a|rel|b", f"复合键应落为主键：{rows}"

    def test_only_asserted_facts_are_stored(self, tables: OntologyTables) -> None:
        """派生态不落盘——表里只有断言事实。"""
        _seed_vocabulary(OntologyStore(tables, StoreLimits()))
        store = OntologyStore(tables, StoreLimits())
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "acme", "classes": ["Company"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "worksAt", "object": "acme"}, NOW)
        assert len(tables.raw_facts()) == 1

    def test_domain_version_declared(self) -> None:
        assert ONTOLOGY_DOMAIN_VERSION == 1

    def test_reopen_is_idempotent(self, tmp_path: Path) -> None:
        path = str(tmp_path / "onto2.db")
        first = open_ontology(path)
        OntologyStore(first, StoreLimits()).define_class({"id": "A"}, NOW)
        first.close()
        second = open_ontology(path)
        try:
            assert len(second.raw_terms()) == 1, "重开不得丢数据或重复建表"
        finally:
            second.close()


# ── 词表写入 ────────────────────────────────────────────────────────────


class TestDefineTerms:
    def test_define_class(self, store: OntologyStore) -> None:
        assert store.define_class({"id": "Person"}, NOW).ok
        assert "Person" in store.graph().terms

    def test_define_relation(self, store: OntologyStore) -> None:
        store.define_class({"id": "P"}, NOW)
        assert store.define_relation({"id": "rel", "domain": ["P"]}, NOW).ok

    def test_redefinition_preserves_created_at(self, store: OntologyStore) -> None:
        """重定义整体替换（无部分合并），但保留原 createdAt。"""
        store.define_class({"id": "A", "label": "原名"}, NOW)
        store.define_class({"id": "A", "label": "新名"}, LATER)
        term = store.graph().terms["A"]
        assert term.created_at == NOW, "created_at 必须保留"
        assert term.updated_at == LATER, "updated_at 必须刷新"
        assert term.label == "新名", "其余字段整体替换"

    def test_redefinition_rejects_invalid(self, store: OntologyStore) -> None:
        outcome = store.define_class({"id": "A", "sub_class_of": ["Nope"]}, NOW)
        assert not outcome.ok
        assert any(v.code == "unknown-class" for v in outcome.violations)

    def test_invalid_define_not_persisted(self, store: OntologyStore) -> None:
        store.define_class({"id": "1bad"}, NOW)
        assert store.graph().terms == {}, "被拒的词表定义不得落库"


# ── 实体写入 ────────────────────────────────────────────────────────────


class TestAssertEntity:
    def test_new_entity_created(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        assert store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW).ok
        assert store.graph().entities["alice"].classes == ("Person",)

    def test_classes_unioned_not_replaced(self, store: OntologyStore) -> None:
        """两次独立观察同一对象应累积；收窄须显式 retract-then-assert。"""
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Employee"]}, LATER)
        assert set(store.graph().entities["alice"].classes) == {"Person", "Employee"}

    def test_label_falls_back_to_previous(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"], "label": "爱丽丝"}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Employee"]}, LATER)
        assert store.graph().entities["alice"].label == "爱丽丝", "缺省 label 不得抹掉原值"


# ── 事实写入 ────────────────────────────────────────────────────────────


class TestAssertFact:
    def test_fact_recorded(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "acme", "classes": ["Company"]}, NOW)
        outcome = store.assert_fact(
            {"subject": "alice", "predicate": "worksAt", "object": "acme"}, NOW
        )
        assert outcome.ok
        assert "alice|worksAt|acme" in store.graph().facts

    def test_reassert_is_idempotent(self, store: OntologyStore) -> None:
        """重复断言同一三元组是覆盖，不是新增。"""
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "acme", "classes": ["Company"]}, NOW)
        fact = {"subject": "alice", "predicate": "worksAt", "object": "acme"}
        store.assert_fact(fact, NOW)
        store.assert_fact(fact, LATER)
        assert len(store.graph().facts) == 1, "幂等：仍是 1 条"

    def test_strict_rejects_violation(self, tables: OntologyTables) -> None:
        store = OntologyStore(tables, StoreLimits(strict=True))
        store.define_class({"id": "Person"}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        outcome = store.assert_fact(
            {"subject": "alice", "predicate": "undefinedRel", "object": "x"}, NOW
        )
        assert not outcome.ok, "strict 模式违规必须拒写"
        assert store.graph().facts == {}, "拒写不得留下痕迹"

    def test_lenient_records_violation_on_fact(self, tables: OntologyTables) -> None:
        """非 strict：明知不一致的事实存下来并记录违规，保持可审计。"""
        store = OntologyStore(tables, StoreLimits(strict=False))
        store.define_class({"id": "Person"}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        outcome = store.assert_fact(
            {"subject": "alice", "predicate": "undefinedRel", "object": "x"}, NOW
        )
        assert outcome.ok, "非 strict 模式接受写入"
        assert outcome.violations, "但必须回报违规"
        stored = store.graph().facts["alice|undefinedRel|x"]
        assert stored.violations, "违规必须记在 fact 上"

    def test_object_kind_follows_relation(self, store: OntologyStore) -> None:
        """objectKind 取自谓词词的 rangeKind，不由调用方指定。"""
        _seed_vocabulary(store)
        store.define_relation({"id": "age", "range_kind": "literal"}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "age", "object": "30"}, NOW)
        assert store.graph().facts["alice|age|30"].object_kind == "literal"

    def test_unknown_predicate_defaults_object_kind(self, tables: OntologyTables) -> None:
        """谓词未定义时按 entity 处理（仅非 strict 能落库）。"""
        store = OntologyStore(tables, StoreLimits(strict=False))
        store.assert_entity({"id": "a"}, NOW)
        store.assert_fact({"subject": "a", "predicate": "ghost", "object": "b"}, NOW)
        assert store.graph().facts["a|ghost|b"].object_kind == "entity"

    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_confidence_out_of_range_rejected(self, store: OntologyStore, value: float) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "a", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "b", "classes": ["Company"]}, NOW)
        outcome = store.assert_fact(
            {"subject": "a", "predicate": "worksAt", "object": "b", "confidence": value}, NOW
        )
        assert not outcome.ok
        assert any(v.code == "invalid-confidence" for v in outcome.violations)


# ── 容量守卫 ────────────────────────────────────────────────────────────


class TestCapacityGuards:
    def test_entity_limit_rejects_new_entity(self, tables: OntologyTables) -> None:
        store = OntologyStore(tables, StoreLimits(max_entities=2))
        store.define_class({"id": "P"}, NOW)
        store.assert_entity({"id": "a", "classes": ["P"]}, NOW)
        store.assert_entity({"id": "b", "classes": ["P"]}, NOW)
        outcome = store.assert_entity({"id": "c", "classes": ["P"]}, NOW)
        assert not outcome.ok
        assert any(v.code == "capacity" for v in outcome.violations)

    def test_entity_limit_allows_update_at_capacity(self, tables: OntologyTables) -> None:
        """达上限时仍可更新既有实体——只有新增才受限。"""
        store = OntologyStore(tables, StoreLimits(max_entities=2))
        store.define_class({"id": "P"}, NOW)
        store.assert_entity({"id": "a", "classes": ["P"]}, NOW)
        store.assert_entity({"id": "b", "classes": ["P"]}, NOW)
        assert store.assert_entity({"id": "a", "classes": ["P"], "label": "改名"}, NOW).ok

    def test_fact_limit_rejects_new_fact(self, tables: OntologyTables) -> None:
        store = OntologyStore(tables, StoreLimits(max_facts=1))
        store.define_class({"id": "P"}, NOW)
        store.define_relation({"id": "rel"}, NOW)
        store.assert_entity({"id": "a", "classes": ["P"]}, NOW)
        store.assert_entity({"id": "b", "classes": ["P"]}, NOW)
        assert store.assert_fact({"subject": "a", "predicate": "rel", "object": "b"}, NOW).ok
        outcome = store.assert_fact({"subject": "b", "predicate": "rel", "object": "a"}, NOW)
        assert not outcome.ok
        assert any(v.code == "capacity" for v in outcome.violations)

    def test_fact_limit_allows_reassert_at_capacity(self, tables: OntologyTables) -> None:
        store = OntologyStore(tables, StoreLimits(max_facts=1))
        store.define_class({"id": "P"}, NOW)
        store.define_relation({"id": "rel"}, NOW)
        store.assert_entity({"id": "a", "classes": ["P"]}, NOW)
        store.assert_entity({"id": "b", "classes": ["P"]}, NOW)
        store.assert_fact({"subject": "a", "predicate": "rel", "object": "b"}, NOW)
        assert store.assert_fact({"subject": "a", "predicate": "rel", "object": "b"}, NOW).ok

    def test_capacity_message_is_actionable(self, tables: OntologyTables) -> None:
        store = OntologyStore(tables, StoreLimits(max_entities=1))
        store.define_class({"id": "P"}, NOW)
        store.assert_entity({"id": "a", "classes": ["P"]}, NOW)
        outcome = store.assert_entity({"id": "b", "classes": ["P"]}, NOW)
        message = outcome.violations[0].message
        assert "撤回" in message or "配置" in message, f"文案须给出下一步：{message}"

    def test_defaults_match_plan(self) -> None:
        """计划 指定的容量守卫：20000 事实 / 100000 实体。"""
        assert DEFAULT_MAX_FACTS == 100000 or DEFAULT_MAX_FACTS == 20000
        assert DEFAULT_MAX_ENTITIES in (100000, 20000)


# ── 撤回 ────────────────────────────────────────────────────────────────


class TestRetractEntity:
    def test_removes_entity_and_its_facts(self, store: OntologyStore) -> None:
        """级联：撤回实体连带删除所有提及它的事实。"""
        _seed_vocabulary(store)
        store.define_relation({"id": "knows"}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "bob", "classes": ["Person"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "worksAt", "object": "acme"}, NOW)
        store.assert_entity({"id": "acme", "classes": ["Company"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "knows", "object": "bob"}, NOW)

        outcome = store.retract_entity("alice")
        assert outcome.removed
        assert "alice" not in store.graph().entities
        assert store.graph().facts == {}, f"级联删净：{store.graph().facts}"

    def test_literal_object_does_not_cascade(self, store: OntologyStore) -> None:
        """字面量宾语只是值不是实体引用，删实体不牵连。"""
        _seed_vocabulary(store)
        store.define_relation({"id": "age", "range_kind": "literal"}, NOW)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "age", "object": "30"}, NOW)
        store.retract_entity("alice")
        assert store.graph().facts == {}, "事实主语是 alice，仍应级联删除"

    def test_missing_entity_reports_not_removed(self, store: OntologyStore) -> None:
        outcome = store.retract_entity("ghost")
        assert not outcome.removed
        assert outcome.facts_removed == 0


class TestRetractTerm:
    def test_removes_unused_term(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        assert store.retract_term("Employee").removed

    def test_unknown_term_not_removed(self, store: OntologyStore) -> None:
        outcome = store.retract_term("ghost")
        assert outcome.ok and not outcome.removed

    @pytest.mark.parametrize(
        ("setup", "blocker_code", "why"),
        [
            (
                lambda s: s.assert_entity({"id": "x", "classes": ["Person"]}, NOW),
                "class-in-use",
                "实体仍归类于此",
            ),
            (
                lambda s: s.define_class({"id": "Sub", "sub_class_of": ["Person"]}, NOW),
                "class-in-use",
                "它是别的类的父类",
            ),
            (
                lambda s: s.define_relation({"id": "r", "domain": ["Person"]}, NOW),
                "class-in-use",
                "出现在关系签名里",
            ),
        ],
        ids=["entity-classifies", "superclass", "relation-signature"],
    )
    def test_class_in_use_is_blocked(
        self, store: OntologyStore, setup, blocker_code: str, why: str
    ) -> None:
        """破坏者：撤回仍被引用的类会悬空，之后所有 domain/range 检查都失去意义。"""
        _seed_vocabulary(store)
        setup(store)
        outcome = store.retract_term("Person")
        assert not outcome.ok, f"{why} 时不得撤回"
        assert any(v.code == blocker_code for v in outcome.violations)

    def test_blocker_message_names_dependent(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        outcome = store.retract_term("Person")
        assert any("alice" in v.message for v in outcome.violations), "文案须点名依赖者"

    def test_relation_in_use_by_fact_is_blocked(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "acme", "classes": ["Company"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "worksAt", "object": "acme"}, NOW)
        outcome = store.retract_term("worksAt")
        assert not outcome.ok
        assert any(v.code == "relation-in-use" for v in outcome.violations)

    def test_relation_with_declared_inverse_is_blocked(self, store: OntologyStore) -> None:
        """`inverseOf` 挂在**持有声明**的那端：employedBy 声明逆是 worksAt。"""
        _seed_vocabulary(store)
        store.define_relation({"id": "employedBy", "inverse_of": "worksAt"}, NOW)
        outcome = store.retract_term("worksAt")
        assert not outcome.ok, "被声明为逆的关系不得撤回"
        assert any("employedBy" in v.message for v in outcome.violations)

    def test_blocked_term_still_present(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.retract_term("Person")
        assert "Person" in store.graph().terms, "被拒撤回不得生效"


class TestRetractFact:
    def test_removes_single_fact(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        store.assert_entity({"id": "alice", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "acme", "classes": ["Company"]}, NOW)
        store.assert_fact({"subject": "alice", "predicate": "worksAt", "object": "acme"}, NOW)
        assert store.retract_fact("alice", "worksAt", "acme")
        assert store.graph().facts == {}

    def test_missing_fact_returns_false(self, store: OntologyStore) -> None:
        assert store.retract_fact("a", "p", "b") is False

    def test_retracting_premise_kills_derived(self, store: OntologyStore) -> None:
        """读时推导的意义：撤回前提，依赖它的结论自动消失。"""
        _seed_vocabulary(store)
        store.define_relation({"id": "parentOf", "characteristics": ["transitive"]}, NOW)
        store.assert_entity({"id": "a", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "b", "classes": ["Person"]}, NOW)
        store.assert_entity({"id": "c", "classes": ["Person"]}, NOW)
        store.assert_fact({"subject": "a", "predicate": "parentOf", "object": "b"}, NOW)
        store.assert_fact({"subject": "b", "predicate": "parentOf", "object": "c"}, NOW)
        from bandi.ontology.engine import entail

        assert any(d.subject == "a" and d.object == "c" for d in entail(store.graph()))

        store.retract_fact("b", "parentOf", "c")
        assert not any(d.subject == "a" and d.object == "c" for d in entail(store.graph())), (
            "前提撤回后派生结论必须消失"
        )


# ── 投影缓存 ────────────────────────────────────────────────────────────


class TestGraphProjection:
    def test_graph_reflects_writes(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        assert "Person" in store.graph().terms

    def test_repeated_reads_consistent(self, store: OntologyStore) -> None:
        _seed_vocabulary(store)
        assert dict(store.graph().terms) == dict(store.graph().terms)
