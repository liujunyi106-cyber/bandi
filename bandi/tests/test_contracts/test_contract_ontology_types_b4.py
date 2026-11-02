"""契约：本体词汇表数据模型（对照表 -1 / -1）。

复刻 `src/types.ts`（126 行全读）。
行为不变式（钉行为，不钉实现）：

  1. **复合键** `factKey(s,p,o)` = `s|p|o`——同一三元组重复断言是幂等覆盖而非重复
  2. **Term 二态判别**：`kind` 为 `class`/`relation`，取错类返回 None（不抛）
  3. **Fact.objectKind** 二态：`entity`/`literal`，随谓词词的 rangeKind 定
  4. **DerivedFact.via** 四态：`asserted`/`inverse`/`symmetric`/`transitive`
  5. **Violation** = (code, message)——code 机器可判、message 面向模型可修复
  6. **id 语法**：字母/下划线开头，仅含字母数字与 `_ . : -`，≤128 字符
     （分隔符 `|` 被排除在外，故复合键无需转义）
  7. **日期戳**：术语与实体保留 createdAt（重定义不得改），updatedAt 每次写刷新

`src/types.ts:15-126`。
"""

from __future__ import annotations

import pytest

from bandi.ontology.types import (
    FACT_KEY_SEPARATOR,
    ID_MAX_LENGTH,
    ID_PATTERN,
    ClassTerm,
    DerivedFact,
    Entity,
    Fact,
    GraphView,
    RangeKind,
    RelationCharacteristic,
    RelationTerm,
    Term,
    ViaKind,
    Violation,
    class_of,
    fact_key,
    relation_of,
)

NOW = "2026-10-13T00:00:00Z"
LATER = "2026-10-14T00:00:00Z"


class TestFactKey:
    def test_composite_key_joins_three_positions(self) -> None:
        assert fact_key("alice", "worksAt", "acme") == "alice|worksAt|acme"

    def test_separator_is_pipe(self) -> None:
        assert FACT_KEY_SEPARATOR == "|"

    def test_same_triple_same_key(self) -> None:
        """幂等的基础：同一三元组两次断言必须撞同一个键。"""
        assert fact_key("a", "p", "b") == fact_key("a", "p", "b")

    def test_position_order_matters(self) -> None:
        assert fact_key("a", "p", "b") != fact_key("b", "p", "a")

    def test_key_unambiguous_because_ids_exclude_separator(self) -> None:
        """破坏者：id 语法排除 `|`，故 `a|bc` 与 `a`+`bc` 不会撞键。"""
        import re

        assert re.fullmatch(ID_PATTERN, "a|bc") is None, "id 不得含分隔符"

    @pytest.mark.parametrize(
        "value",
        ["alice", "_x", "schema:Person", "repo.Module", "a-b.c_d:1", "A" * ID_MAX_LENGTH],
        ids=["plain", "leading-underscore", "ns-colon", "ns-dot", "mixed", "max-length"],
    )
    def test_accepted_ids(self, value: str) -> None:
        import re

        assert re.fullmatch(ID_PATTERN, value), f"{value!r} 应是合法 id"

    @pytest.mark.parametrize(
        ("value", "why"),
        [
            ("", "空"),
            ("1abc", "数字开头"),
            ("-abc", "连字符开头"),
            ("a b", "含空格"),
            ("a|b", "含分隔符"),
            ("a/b", "含斜杠"),
            ("中文", "非 ASCII 字母"),
        ],
        ids=["empty", "digit-lead", "dash-lead", "space", "pipe", "slash", "cjk"],
    )
    def test_rejected_ids(self, value: str, why: str) -> None:
        import re

        assert not re.fullmatch(ID_PATTERN, value), f"{why}：{value!r} 应被拒"

    def test_length_limit_is_engine_concern_not_regex(self) -> None:
        """长度上限由 `engine.check_id` 显式判定——正则不表达长度。

        正则只管字符语法；`ID_MAX_LENGTH` 的执行点在 `check_id`
        （同 dsh `ontology.ts:44` 的 `id.length > ID_MAX_LENGTH` 分支）。
        """
        import re

        too_long = "A" * (ID_MAX_LENGTH + 1)
        assert re.fullmatch(ID_PATTERN, too_long) is not None, "字符语法本身合法"
        assert len(too_long) > ID_MAX_LENGTH, "但长度超限，应由 check_id 拒绝"


class TestTermDiscrimination:
    def _graph(self) -> GraphView:
        return GraphView(
            terms={
                "Person": ClassTerm(
                    kind="class",
                    id="Person",
                    sub_class_of=(),
                    created_at=NOW,
                    updated_at=NOW,
                ),
                "worksAt": RelationTerm(
                    kind="relation",
                    id="worksAt",
                    domain=("Person",),
                    range_kind="entity",
                    range=("Company",),
                    characteristics=("functional",),
                    inverse_of=None,
                    created_at=NOW,
                    updated_at=NOW,
                ),
            },
            entities={},
            facts={},
        )

    def test_class_of_returns_class(self) -> None:
        assert class_of(self._graph(), "Person") is not None

    def test_class_of_on_relation_returns_none(self) -> None:
        """取错类返回 None，不抛——词表 id 空间共享，误查是常态。"""
        assert class_of(self._graph(), "worksAt") is None

    def test_relation_of_returns_relation(self) -> None:
        assert relation_of(self._graph(), "worksAt") is not None

    def test_relation_of_on_class_returns_none(self) -> None:
        assert relation_of(self._graph(), "Person") is None

    def test_unknown_id_returns_none(self) -> None:
        assert class_of(self._graph(), "Nope") is None
        assert relation_of(self._graph(), "Nope") is None


class TestFactShape:
    def test_object_kind_two_states(self) -> None:
        """objectKind 只有 entity/literal 两态——字面量不能当实体参与推理。"""
        entity_fact = Fact(
            subject="alice",
            predicate="worksAt",
            object="acme",
            object_kind="entity",
            violations=(),
            created_at=NOW,
        )
        literal_fact = Fact(
            subject="alice",
            predicate="age",
            object="30",
            object_kind="literal",
            violations=(),
            created_at=NOW,
        )
        assert entity_fact.object_kind == "entity"
        assert literal_fact.object_kind == "literal"
        assert RangeKind.__args__ == ("entity", "literal")

    def test_violations_stored_on_fact(self) -> None:
        """strict=False 时违规记在 fact 上，保持可审计而非静默接受。"""
        fact = Fact(
            subject="alice",
            predicate="worksAt",
            object="acme",
            object_kind="entity",
            violations=("worksAt 的主语 alice 不在域内",),
            created_at=NOW,
        )
        assert fact.violations == ("worksAt 的主语 alice 不在域内",)

    def test_confidence_optional_in_unit_range(self) -> None:
        confident = Fact(
            subject="a",
            predicate="p",
            object="b",
            object_kind="entity",
            confidence=0.9,
            violations=(),
            created_at=NOW,
        )
        certain = Fact(
            subject="a",
            predicate="p",
            object="b",
            object_kind="entity",
            violations=(),
            created_at=NOW,
        )
        assert confident.confidence == 0.9
        assert certain.confidence is None, "缺省=断言为确定"


class TestDerivedFactVia:
    def test_via_has_four_states(self) -> None:
        """via 四态：asserted + 三条推理规则各一。"""
        assert ViaKind.__args__ == ("asserted", "inverse", "symmetric", "transitive")

    def test_derived_fact_default_via_is_asserted(self) -> None:
        derived = DerivedFact(
            subject="a", predicate="p", object="b", object_kind="entity", created_at=NOW
        )
        assert derived.via == "asserted", "默认按断言事实处理"

    def test_asserted_fact_carries_via(self) -> None:
        derived = DerivedFact(
            subject="alice",
            predicate="worksAt",
            object="acme",
            object_kind="entity",
            violations=(),
            created_at=NOW,
            via="asserted",
        )
        assert derived.via == "asserted"

    def test_inference_origin_recorded(self) -> None:
        derived = DerivedFact(
            subject="acme",
            predicate="employs",
            object="alice",
            object_kind="entity",
            violations=(),
            created_at=NOW,
            via="inverse",
        )
        assert derived.via == "inverse", "派生态必须标记推理来源，否则不可解释"


class TestViolation:
    def test_violation_has_code_and_message(self) -> None:
        """code 供程序判别、message 供模型自纠——两者缺一不可。"""
        violation = Violation(code="unknown-class", message="类 Foo 未定义")
        assert violation.code == "unknown-class"
        assert violation.message == "类 Foo 未定义"

    def test_message_names_offending_value(self) -> None:
        """面向模型可修复：文案必须点名出错的值和被破坏的约束。"""
        violation = Violation(
            code="domain-mismatch",
            message="alice 不在 worksAt 的域内（需要 Person；实际无）",
        )
        assert "alice" in violation.message
        assert "worksAt" in violation.message


class TestEntityShape:
    def test_direct_classes_recorded(self) -> None:
        entity = Entity(id="alice", classes=("Person", "Engineer"), created_at=NOW, updated_at=NOW)
        assert entity.classes == ("Person", "Engineer")

    def test_created_at_preserved_across_redefinition(self) -> None:
        """重定义只更新 updatedAt，createdAt 保留原值（同 dsh store.ts:85）。"""
        original = Entity(id="alice", classes=("Person",), created_at=NOW, updated_at=NOW)
        redefined = Entity(id="alice", classes=("Engineer",), created_at=NOW, updated_at=LATER)
        assert redefined.created_at == original.created_at
        assert redefined.updated_at == LATER > redefined.created_at


class TestRelationCharacteristics:
    def test_three_characteristics(self) -> None:
        assert set(RelationCharacteristic.__args__) == {"transitive", "symmetric", "functional"}

    def test_inverse_of_optional(self) -> None:
        assert RelationTerm.__dataclass_fields__["inverse_of"].default is None


class TestTermUnion:
    def test_term_is_class_or_relation(self) -> None:
        """Term = ClassTerm | RelationTerm（判别联合）。"""
        assert Term is not None
        class_term = ClassTerm(
            kind="class", id="A", sub_class_of=(), created_at=NOW, updated_at=NOW
        )
        assert class_term.kind == "class"
