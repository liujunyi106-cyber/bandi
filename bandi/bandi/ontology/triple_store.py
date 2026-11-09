"""本体写入语义。

三张表与纯推理核心之间的薄 IO 层：把表投影成 `GraphView`，应用经校验的写入，
执行容量守卫。所有规则在 `engine.py`，本模块只决定什么该到达存储。

写入语义要点：
- 词表重定义整体替换但保留 `createdAt`（无部分合并——半更新会让词表自相矛盾）
- 实体 classes 取并集（两次独立观察应累积；收窄须显式 retract-then-assert）
- 事实按复合键幂等覆盖（重复断言不是重复行）
- `strict=False` 时违规记在 fact 上，让明知不一致的断言保持可审计
- 撤回实体级联删事实；撤回词表项在仍被依赖时拒绝"""

from __future__ import annotations

from dataclasses import dataclass, field

from bandi.ontology.engine import (
    validate_class_input,
    validate_entity_input,
    validate_fact_input,
    validate_relation_input,
)
from bandi.ontology.spec import OntologyTables, build_graph_view
from bandi.ontology.types import (
    ClassTerm,
    Entity,
    Fact,
    GraphView,
    RelationTerm,
    Term,
    Violation,
    fact_key,
    relation_of,
)

# 容量守卫：事实 20000 / 实体 100000
DEFAULT_MAX_FACTS = 20000
DEFAULT_MAX_ENTITIES = 100000


@dataclass
class StoreLimits:
    """功能：写入前由存储执行的容量与严格度策略。"""

    max_facts: int = DEFAULT_MAX_FACTS
    max_entities: int = DEFAULT_MAX_ENTITIES
    strict: bool = True


@dataclass
class WriteOutcome:
    """功能：一次写入尝试的结果。"""

    ok: bool
    violations: list[Violation] = field(default_factory=list)

    @property
    def codes(self) -> set[str]:
        """功能：违规码集合（契约与日志判定用）。"""
        return {v.code for v in self.violations}


@dataclass
class RetractEntityOutcome:
    """功能：撤回实体的结果——是否删除、连带多少条事实。"""

    removed: bool
    facts_removed: int = 0


@dataclass
class RetractTermOutcome:
    """功能：撤回词表项的结果——`ok=False` 表示被依赖者挡住。"""

    ok: bool
    violations: list[Violation] = field(default_factory=list)
    removed: bool = False


class OntologyStore:
    """功能：一个已打开本体库之上的活跃本体。

    读路径把三表投影成不可变 `GraphView` 并按写代次记忆化——一次工具调用里的
    一串查询只付一次投影代价。写路径走与工具暴露的同一批校验器，故没有任何内容
    能未经验证到达存储。
    """

    def __init__(self, tables: OntologyTables, limits: StoreLimits | None = None) -> None:
        self._tables = tables
        self._limits = limits or StoreLimits()
        self._cache: GraphView | None = None

    @property
    def tables(self) -> OntologyTables:
        """功能：底层三表（迁移脚本用）。"""
        return self._tables

    @property
    def limits(self) -> StoreLimits:
        """功能：当前容量与严格度策略。"""
        return self._limits

    def graph(self) -> GraphView:
        """功能：当前图——每个写代次只投影一次。"""
        if self._cache is None:
            self._cache = build_graph_view(self._tables)
        return self._cache

    def close(self) -> None:
        """功能：关闭底层连接。"""
        self._tables.close()

    def _commit(self) -> None:
        """功能：提交写入并作废投影缓存。"""
        self._tables.commit()
        self._cache = None

    # -- 词表写入 --------------------------------------------------------

    def define_class(self, input: dict, now: str) -> WriteOutcome:
        """功能：定义或重定义类——整体替换，保留原 `created_at`。"""
        violations = validate_class_input(self.graph(), input)
        if violations:
            return WriteOutcome(False, violations)
        previous = self._tables.get_term(input["id"])
        term: Term = ClassTerm(
            kind="class",
            id=input["id"],
            label=input.get("label"),
            comment=input.get("comment"),
            sub_class_of=tuple(input.get("sub_class_of") or ()),
            created_at=previous.created_at if isinstance(previous, ClassTerm) else now,
            updated_at=now,
        )
        self._tables.put_term(term, now)
        self._commit()
        return WriteOutcome(True)

    def define_relation(self, input: dict, now: str) -> WriteOutcome:
        """功能：定义或重定义关系——整体替换，保留原 `created_at`。"""
        violations = validate_relation_input(self.graph(), input)
        if violations:
            return WriteOutcome(False, violations)
        previous = self._tables.get_term(input["id"])
        term = RelationTerm(
            kind="relation",
            id=input["id"],
            label=input.get("label"),
            comment=input.get("comment"),
            domain=tuple(input.get("domain") or ()),
            range_kind=input.get("range_kind", "entity"),
            range=tuple(input.get("range") or ()),
            characteristics=tuple(input.get("characteristics") or ()),
            inverse_of=input.get("inverse_of"),
            created_at=previous.created_at if isinstance(previous, RelationTerm) else now,
            updated_at=now,
        )
        self._tables.put_term(term, now)
        self._commit()
        return WriteOutcome(True)

    # -- 实体写入 --------------------------------------------------------

    def assert_entity(self, input: dict, now: str) -> WriteOutcome:
        """功能：断言实体——已有实体的 classes 取并集而非替换。

        两次对同一个体的独立观察应当累积；收窄是显式的 retract-then-assert。
        """
        violations = validate_entity_input(self.graph(), input)
        if violations:
            return WriteOutcome(False, violations)
        entity_id = input["id"]
        previous = self._tables.get_entity(entity_id)
        if previous is None and len(self.graph().entities) >= self._limits.max_entities:
            return WriteOutcome(
                False,
                [
                    Violation(
                        "capacity",
                        f"实体数已达上限（maxEntities = {self._limits.max_entities}）；"
                        "请先撤回不用的实体或在插件配置中提高上限",
                    )
                ],
            )
        classes = list(
            dict.fromkeys([*(previous.classes if previous else ()), *(input.get("classes") or ())])
        )
        entity = Entity(
            id=entity_id,
            classes=tuple(classes),
            label=input.get("label", previous.label if previous else None),
            comment=input.get("comment", previous.comment if previous else None),
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        self._tables.put_entity(entity, now)
        self._commit()
        return WriteOutcome(True)

    # -- 事实写入 --------------------------------------------------------

    def assert_fact(self, input: dict, now: str) -> WriteOutcome:
        """功能：断言事实——strict 下违规拒写，否则记在 fact 上保持可审计。

        `object_kind` 由谓词词的 `range_kind` 决定，不由调用方指定：让模型自己
        宣称「这个宾语是字面量」会绕开 range 检查。
        """
        violations = validate_fact_input(self.graph(), input)
        if violations and self._limits.strict:
            return WriteOutcome(False, violations)

        key = fact_key(input["subject"], input["predicate"], input["object"])
        if key not in self.graph().facts and len(self.graph().facts) >= self._limits.max_facts:
            return WriteOutcome(
                False,
                [
                    Violation(
                        "capacity",
                        f"事实数已达上限（maxFacts = {self._limits.max_facts}）；"
                        "请撤回陈旧事实或在插件配置中提高上限",
                    )
                ],
            )
        confidence = input.get("confidence")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            return WriteOutcome(
                False,
                [Violation("invalid-confidence", "confidence 必须落在 [0, 1] 区间内")],
            )

        relation = relation_of(self.graph(), input["predicate"])
        object_kind = relation.range_kind if isinstance(relation, RelationTerm) else "entity"
        fact = Fact(
            subject=input["subject"],
            predicate=input["predicate"],
            object=input["object"],
            object_kind=object_kind,
            source=input.get("source"),
            confidence=confidence,
            violations=tuple(v.message for v in violations),
            created_at=now,
        )
        self._tables.put_fact(key, fact)
        self._commit()
        return WriteOutcome(True, list(violations))

    # -- 撤回 ------------------------------------------------------------

    def retract_term(self, term_id: str) -> RetractTermOutcome:
        """功能：撤回词表项——仍有依赖时拒绝。

        悬空的类引用会让之后所有 domain/range 检查失去意义（约束指向不存在的类
        等于没约束），故宁可拒绝并点名依赖者。
        """
        term = self._tables.get_term(term_id)
        if term is None:
            return RetractTermOutcome(True, [], False)

        graph = self.graph()
        blockers: list[Violation] = []
        if isinstance(term, ClassTerm):
            for entity in graph.entities.values():
                if term_id in entity.classes:
                    blockers.append(
                        Violation("class-in-use", f"类 {term_id} 仍在为实体 {entity.id} 归类")
                    )
                    break
            for other in graph.terms.values():
                if isinstance(other, ClassTerm) and term_id in other.sub_class_of:
                    blockers.append(Violation("class-in-use", f"类 {term_id} 是 {other.id} 的父类"))
                    break
                if isinstance(other, RelationTerm) and (
                    term_id in other.domain or term_id in other.range
                ):
                    blockers.append(
                        Violation("class-in-use", f"类 {term_id} 出现在关系 {other.id} 的签名中")
                    )
                    break
        else:
            for fact in graph.facts.values():
                if fact.predicate == term_id:
                    blockers.append(
                        Violation(
                            "relation-in-use",
                            f"关系 {term_id} 仍被断言"
                            f"（例如 {fact.subject} {term_id} {fact.object}）",
                        )
                    )
                    break
            for other in graph.terms.values():
                if (
                    isinstance(other, RelationTerm)
                    and other.inverse_of == term_id
                    and other.id != term_id
                ):
                    blockers.append(
                        Violation("relation-in-use", f"关系 {other.id} 声明 {term_id} 为其逆关系")
                    )
                    break
        if blockers:
            return RetractTermOutcome(False, blockers, False)

        self._tables.delete_term(term_id)
        self._commit()
        return RetractTermOutcome(True, [], True)

    def retract_entity(self, entity_id: str) -> RetractEntityOutcome:
        """功能：撤回实体并删除所有提及它的事实。

        派生事实没有存储键，前提消失后自然不成立。
        """
        existed = self._tables.get_entity(entity_id) is not None
        removed = self._tables.delete_facts_by_entity(entity_id)
        if existed:
            self._tables.delete_entity(entity_id)
        self._commit()
        return RetractEntityOutcome(removed=existed, facts_removed=removed)

    def retract_fact(self, subject: str, predicate: str, object_id: str) -> bool:
        """功能：撤回一条断言事实——返回是否确实删掉了。"""
        key = fact_key(subject, predicate, object_id)
        existed = key in self.graph().facts
        self._tables.delete_fact(key)
        self._commit()
        return existed


__all__ = [
    "DEFAULT_MAX_ENTITIES",
    "DEFAULT_MAX_FACTS",
    "OntologyStore",
    "RetractEntityOutcome",
    "RetractTermOutcome",
    "StoreLimits",
    "WriteOutcome",
]
