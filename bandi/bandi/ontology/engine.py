"""本体推理内核：标识语法、类闭包、TBox 约束校验、关系特征推论、图查询。

复刻 `src/ontology.ts`（749 行）。本模块全是 `GraphView` 的纯函数
无 IO、无存储——故决定「模型这条断言是否可接受」的每条规则都能脱离后端单测。

关键语义：
- 推论跑**不动点**（≤32 轮），故规则可复合（传递关系的逆关系本身也传递闭合）。
- 派生态**永不落盘**——撤回前提时依赖它的结论自动消失，无需反向清理。
- `domain`/`range` 为空是「尚未决定」，不是静默放行。
- 字面量关系不得声明 symmetric/transitive/inverse：派生命题会把字面量放到主语位置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bandi.ontology.types import (
    ID_MAX_LENGTH,
    ID_PATTERN,
    ClassTerm,
    DerivedFact,
    Entity,
    GraphView,
    RelationTerm,
    Term,
    ViaKind,
    Violation,
    class_of,
    fact_key,
    relation_of,
)

# 推论不动点轮数上限——成环图否则会无限迭代
MAX_ENTAILMENT_ROUNDS = 32
# 默认派生事实上限
DEFAULT_ENTAILMENT_LIMIT = 20000

_ID_RE = re.compile(ID_PATTERN)


# ---------------------------------------------------------------------------
# 标识与键
# ---------------------------------------------------------------------------


def check_id(value: str, what: str) -> Violation | None:
    """功能：标识符校验——字符语法 + 长度上限。`what` 用于文案（class/entity…）。"""
    text = str(value or "")
    if not text:
        return Violation("invalid-id", f"{what} id 不得为空")
    if len(text) > ID_MAX_LENGTH:
        return Violation(
            "invalid-id",
            f"{what} id {text!r} 超过 {ID_MAX_LENGTH} 字符上限",
        )
    if not _ID_RE.fullmatch(text):
        return Violation(
            "invalid-id",
            f"{what} id {text!r} 必须以字母或下划线开头，且只含字母、数字与 `_ . : -`",
        )
    return None


# ---------------------------------------------------------------------------
# 类闭包
# ---------------------------------------------------------------------------


def class_ancestors(terms: dict[str, Term], class_id: str) -> set[str]:
    """功能：从一个类沿 `sub_class_of` 上溯——含自身，环安全（visited 兼结果集）。"""
    seen: set[str] = set()
    queue = [class_id]
    while queue:
        current = queue.pop()
        if not current or current in seen:
            continue
        seen.add(current)
        term = terms.get(current)
        if not isinstance(term, ClassTerm):
            continue
        queue.extend(term.sub_class_of)
    return seen


def class_descendants(terms: dict[str, Term], class_id: str) -> set[str]:
    """功能：被某个类涵盖的全部类（含自身）——对直接父边跑不动点。"""
    result = {class_id}
    grew = True
    while grew:
        grew = False
        for term in terms.values():
            if not isinstance(term, ClassTerm) or term.id in result:
                continue
            if any(parent in result for parent in term.sub_class_of):
                result.add(term.id)
                grew = True
    return result


def entity_classes(graph: GraphView, entity_id: str) -> set[str]:
    """功能：实体的有效类归属——直接类并上其 `sub_class_of` 传递闭包。"""
    entity = graph.entities.get(entity_id)
    if entity is None:
        return set()
    result: set[str] = set()
    for direct in entity.classes:
        result |= class_ancestors(graph.terms, direct)
    return result


def _describe_classes(classes: set[str]) -> str:
    """功能：把类集合渲染进报错文案（空集显示 none）。"""
    return "none" if not classes else ", ".join(sorted(classes))


# ---------------------------------------------------------------------------
# TBox 校验
# ---------------------------------------------------------------------------


def validate_class_input(graph: GraphView, input: dict) -> list[Violation]:
    """功能：类定义校验——id 语法、与既有关系撞名、未知父类、`subClassOf` 成环。"""
    violations: list[Violation] = []
    violation = check_id(input.get("id", ""), "class")
    if violation:
        violations.append(violation)

    existing = graph.terms.get(input.get("id", ""))
    if existing is not None and not isinstance(existing, ClassTerm):
        violations.append(
            Violation(
                "kind-conflict",
                f"{input.get('id')} 已定义为关系；须先撤回才能改定义为类",
            )
        )

    for parent in input.get("sub_class_of") or ():
        term = graph.terms.get(parent)
        if term is None:
            violations.append(
                Violation("unknown-class", f"{input.get('id')} 的父类 {parent} 未定义")
            )
            continue
        if not isinstance(term, ClassTerm):
            violations.append(
                Violation("kind-conflict", f"{input.get('id')} 的父类 {parent} 是关系，不是类")
            )
            continue
        # 父类已涵盖新类时，这条边会闭合成环
        if parent == input.get("id") or input.get("id") in class_ancestors(graph.terms, parent):
            violations.append(
                Violation(
                    "subclass-cycle",
                    f"{input.get('id')} subClassOf {parent} 会造成继承环",
                )
            )
    return violations


def validate_relation_input(graph: GraphView, input: dict) -> list[Violation]:
    """功能：关系定义校验——含两条保证推论可靠的一致性规则：

    字面量值关系不得声明 symmetric/transitive/inverse（其对象不是实体，派生的
    三元组会把字面量放在主语位置）；声明的逆关系本身必须是实体值关系。
    """
    violations: list[Violation] = []
    rel_id = input.get("id", "")
    violation = check_id(rel_id, "relation")
    if violation:
        violations.append(violation)

    existing = graph.terms.get(rel_id)
    if existing is not None and not isinstance(existing, RelationTerm):
        violations.append(
            Violation("kind-conflict", f"{rel_id} 已定义为类；须先撤回才能改定义为关系")
        )

    range_kind = input.get("range_kind", "entity")
    for role in ("domain", "range"):
        ids = input.get(role) or ()
        if role == "range" and range_kind == "literal" and ids:
            violations.append(
                Violation(
                    "literal-range",
                    f'{rel_id} 的 rangeKind 是 "literal"，不能再约束 range 类',
                )
            )
            continue
        for class_id in ids:
            if class_of(graph, class_id) is None:
                violations.append(
                    Violation(
                        "unknown-class",
                        f"关系 {rel_id} 的 {role} 类 {class_id} 未定义",
                    )
                )

    characteristics = input.get("characteristics") or ()
    if range_kind == "literal":
        for characteristic in characteristics:
            if characteristic in ("symmetric", "transitive"):
                violations.append(
                    Violation(
                        "literal-characteristic",
                        f"{rel_id} 是字面量值关系，不能是 {characteristic}"
                        "（派生的三元组会以字面量作主语）",
                    )
                )
        if input.get("inverse_of") is not None:
            violations.append(
                Violation("literal-inverse", f"{rel_id} 是字面量值关系，不能声明逆关系")
            )

    if input.get("inverse_of") is not None and range_kind == "entity":
        inverse_id = input["inverse_of"]
        inverse = graph.terms.get(inverse_id)
        if inverse is None:
            # 自逆是唯一值得放行的前向引用——否则一对互逆关系须分两次定义
            if inverse_id != rel_id:
                violations.append(
                    Violation(
                        "unknown-relation",
                        f"{rel_id} 的逆关系 {inverse_id} 未定义；请先定义它",
                    )
                )
        elif not isinstance(inverse, RelationTerm):
            violations.append(
                Violation("kind-conflict", f"{rel_id} 的逆关系 {inverse_id} 是类，不是关系")
            )
        elif inverse.range_kind == "literal":
            violations.append(
                Violation(
                    "literal-inverse",
                    f"{rel_id} 的逆关系 {inverse_id} 是字面量值关系，不能被取逆",
                )
            )
    return violations


def validate_entity_input(graph: GraphView, input: dict) -> list[Violation]:
    """功能：实体断言校验——id 语法 + 每个声明的类都必须已存在为类。"""
    violations: list[Violation] = []
    violation = check_id(input.get("id", ""), "entity")
    if violation:
        violations.append(violation)
    for class_id in input.get("classes") or ():
        if class_of(graph, class_id) is None:
            violations.append(
                Violation("unknown-class", f"实体 {input.get('id')} 的类 {class_id} 未定义")
            )
    return violations


def validate_fact_input(graph: GraphView, input: dict) -> list[Violation]:
    """功能：事实校验——这是把本插件与自由文本记忆区分开的那条约束。

    检查顺序：谓词已定义关系 → 主语是已知实体且落在 domain 内 → 宾语匹配
    rangeKind 与 range → `functional` 关系不得已有不同宾语。
    """
    violations: list[Violation] = []

    violation = check_id(input.get("subject", ""), "subject")
    if violation:
        violations.append(violation)

    relation = relation_of(graph, input.get("predicate", ""))
    if relation is None:
        violations.append(
            Violation(
                "unknown-relation",
                f"谓词 {input.get('predicate')} 不是已定义的关系；请先用 ontology_define 声明",
            )
        )
        # 其余规则都要读关系——就此打住，不叠加噪音违规
        return violations

    subject = input.get("subject", "")
    object_id = input.get("object", "")

    if subject not in graph.entities:
        violations.append(
            Violation("unknown-entity", f"主语 {subject} 不是已知实体；请先断言该实体")
        )
    elif relation.domain:
        subject_classes = entity_classes(graph, subject)
        if not any(allowed in subject_classes for allowed in relation.domain):
            violations.append(
                Violation(
                    "domain-mismatch",
                    f"{subject} 不在关系 {relation.id} 的域内"
                    f"（需要其一：{', '.join(relation.domain)}；实有："
                    f"{_describe_classes(subject_classes)}）",
                )
            )

    if relation.range_kind == "literal":
        if not object_id:
            violations.append(Violation("empty-literal", f"{relation.id} 需要字面量值，但宾语为空"))
    elif object_id not in graph.entities:
        violations.append(
            Violation(
                "unknown-entity",
                f"宾语 {object_id} 不是已知实体；请先断言该实体（{relation.id} 是实体值关系）",
            )
        )
    elif relation.range:
        object_classes = entity_classes(graph, object_id)
        if not any(allowed in object_classes for allowed in relation.range):
            violations.append(
                Violation(
                    "range-mismatch",
                    f"{object_id} 不在关系 {relation.id} 的值域内"
                    f"（需要其一：{', '.join(relation.range)}；实有："
                    f"{_describe_classes(object_classes)}）",
                )
            )

    if "functional" in relation.characteristics:
        for fact in graph.facts.values():
            if (
                fact.subject == subject
                and fact.predicate == relation.id
                and fact.object != object_id
            ):
                violations.append(
                    Violation(
                        "functional-conflict",
                        f"{relation.id} 是函数型，{subject} 已有 {relation.id} = "
                        f"{fact.object}；请先撤回那条事实",
                    )
                )
                break
    return violations


# ---------------------------------------------------------------------------
# 推论
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Derived:
    """功能：推论过程内的轻量事实表示——带 `via` 以便标注推理来源。"""

    subject: str
    predicate: str
    object: str
    via: ViaKind

    def to_fact(self) -> DerivedFact:
        return DerivedFact(
            subject=self.subject,
            predicate=self.predicate,
            object=self.object,
            object_kind="entity",
            created_at="",
            via=self.via,
        )


def entail(graph: GraphView, limit: int = DEFAULT_ENTAILMENT_LIMIT) -> list[DerivedFact]:
    """功能：按关系的声明特征推导出所有结论事实——跑到不动点（≤32 轮）。

    派生态永不持久化——撤回前提时依赖它的结论随之消失，这正是读时推导的意义。
    """
    derived: dict[str, _Derived] = {}
    asserted = frozenset(graph.facts.keys())

    def _add(fact: _Derived) -> bool:
        key = fact_key(fact.subject, fact.predicate, fact.object)
        if key in asserted or key in derived or len(derived) >= limit:
            return False
        derived[key] = fact
        return True

    base = [_Derived(f.subject, f.predicate, f.object, "asserted") for f in graph.facts.values()]
    for _round in range(MAX_ENTAILMENT_ROUNDS):
        grew = False
        current = [*base, *derived.values()]
        for fact in current:
            relation = relation_of(graph, fact.predicate)
            if relation is None or relation.range_kind != "entity":
                continue
            if relation.inverse_of and relation.inverse_of != relation.id:
                grew = (
                    _add(_Derived(fact.object, relation.inverse_of, fact.subject, "inverse"))
                    or grew
                )
            if "symmetric" in relation.characteristics:
                grew = (
                    _add(_Derived(fact.object, fact.predicate, fact.subject, "symmetric")) or grew
                )
            if "transitive" in relation.characteristics:
                for nxt in current:
                    if nxt.predicate != fact.predicate or nxt.subject != fact.object:
                        continue
                    if nxt.object == fact.subject:
                        continue  # 绕一圈回来不产生自环
                    grew = (
                        _add(_Derived(fact.subject, fact.predicate, nxt.object, "transitive"))
                        or grew
                    )
        if not grew:
            break

    return [fact.to_fact() for fact in derived.values()]


def all_facts(graph: GraphView, include_inferred: bool) -> list[DerivedFact]:
    """功能：断言事实（可选并入推论）——断言在前。"""
    base = [
        DerivedFact(
            subject=fact.subject,
            predicate=fact.predicate,
            object=fact.object,
            object_kind=fact.object_kind,
            source=fact.source,
            confidence=fact.confidence,
            violations=fact.violations,
            created_at=fact.created_at,
            via="asserted",
        )
        for fact in graph.facts.values()
    ]
    return [*base, *entail(graph)] if include_inferred else base


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def query_entities(graph: GraphView, query: dict) -> list[Entity]:
    """功能：按类归属与/或自由文本找实体——类匹配在子类方向也闭合。

    问 `Service` 会返回声明为其子类 `HttpService` 的实体，反之直接声明为 `Service`
    的实体也命中。
    """
    class_id = query.get("class_id")
    wanted = class_descendants(graph.terms, class_id) if class_id is not None else None
    needle = query.get("search")
    needle = needle.lower() if needle else None
    matches: list[Entity] = []
    for entity in graph.entities.values():
        if wanted is not None and not (entity_classes(graph, entity.id) & wanted):
            continue
        if needle is not None:
            haystack = f"{entity.id}\n{entity.label or ''}\n{entity.comment or ''}".lower()
            if needle not in haystack:
                continue
        matches.append(entity)
    matches.sort(key=lambda e: e.id)
    limit = query.get("limit")
    return matches[:limit] if limit is not None else matches


def query_facts(graph: GraphView, query: dict) -> list[DerivedFact]:
    """功能：三元组模式查询——省略的位置通配。"""
    facts = all_facts(graph, query.get("include_inferred", False))
    matched = [
        fact
        for fact in facts
        if (query.get("subject") is None or fact.subject == query["subject"])
        and (query.get("predicate") is None or fact.predicate == query["predicate"])
        and (query.get("object") is None or fact.object == query["object"])
    ]
    limit = query.get("limit")
    return matched[:limit] if limit is not None else matched


@dataclass
class NeighborResult:
    """功能：邻域查询结果——到达的实体 id 与抵达它们的边。"""

    reached: list[str]
    facts: list[DerivedFact]


def neighbors(
    graph: GraphView, entity_id: str, depth: int, include_inferred: bool
) -> NeighborResult:
    """功能：某个实体的邻域——沿实体值边双向扩散到指定半径。"""
    facts = all_facts(graph, include_inferred)
    reached = {entity_id}
    collected: dict[str, DerivedFact] = {}
    frontier = {entity_id}

    for _step in range(max(1, depth)):
        nxt: set[str] = set()
        for fact in facts:
            outgoing = fact.subject in frontier
            incoming = fact.object_kind == "entity" and fact.object in frontier
            if not outgoing and not incoming:
                continue
            collected[fact_key(fact.subject, fact.predicate, fact.object)] = fact
            if outgoing and fact.object_kind == "entity" and fact.object not in reached:
                nxt.add(fact.object)
            if incoming and fact.subject not in reached:
                nxt.add(fact.subject)
        if not nxt:
            break
        reached |= nxt
        frontier = nxt
    return NeighborResult(reached=sorted(reached), facts=list(collected.values()))


def find_path(
    graph: GraphView, source: str, target: str, max_depth: int, include_inferred: bool
) -> list[DerivedFact] | None:
    """功能：连接两个实体的最短边链——边视为无向，故不论断言方向都能找到。"""
    if source == target:
        return []
    facts = [f for f in all_facts(graph, include_inferred) if f.object_kind == "entity"]
    visited = {source}
    frontier: list[tuple[str, list[DerivedFact]]] = [(source, [])]

    for _step in range(max(1, max_depth)):
        nxt: list[tuple[str, list[DerivedFact]]] = []
        for node_id, path in frontier:
            for fact in facts:
                forward = fact.subject == node_id
                if not forward and fact.object != node_id:
                    continue
                other = fact.object if forward else fact.subject
                if other in visited:
                    continue
                extended = [*path, fact]
                if other == target:
                    return extended
                visited.add(other)
                nxt.append((other, extended))
        if not nxt:
            break
        frontier = nxt
    return None


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------


@dataclass
class GraphStats:
    """功能：图内容计数。"""

    classes: int
    relations: int
    entities: int
    facts: int


def stats_of(graph: GraphView) -> GraphStats:
    """功能：统计图内类/关系/实体/事实数量。"""
    classes = sum(1 for t in graph.terms.values() if isinstance(t, ClassTerm))
    relations = sum(1 for t in graph.terms.values() if isinstance(t, RelationTerm))
    return GraphStats(
        classes=classes,
        relations=relations,
        entities=len(graph.entities),
        facts=len(graph.facts),
    )


def _gloss(term: ClassTerm | RelationTerm) -> str:
    """功能：一个词表项的人读注解（comment 优先于 label）。"""
    text = term.comment or term.label
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return f"  — {collapsed[:120]}"


def render_tbox(graph: GraphView, max_terms: int) -> list[str]:
    """功能：把词表渲染成紧凑行，供系统提示词或 schema 查询用。

    模型需要的是词汇（能断言什么、对什么断言），不是实例数据——后者按需查询。
    顺序稳定（先类后关系，各按 id）使未变的图产生未变的提示词，保持缓存友好。
    """
    ordered = sorted(
        graph.terms.values(),
        key=lambda t: (0 if isinstance(t, ClassTerm) else 1, t.id),
    )
    lines: list[str] = []
    shown = ordered[: max(0, max_terms)]
    for term in shown:
        if isinstance(term, ClassTerm):
            parents = f" < {', '.join(term.sub_class_of)}" if term.sub_class_of else ""
            lines.append(f"class {term.id}{parents}{_gloss(term)}")
        else:
            domain = "|".join(term.domain) or "*"
            target = "literal" if term.range_kind == "literal" else "|".join(term.range) or "*"
            traits = [*term.characteristics]
            if term.inverse_of is not None:
                traits.append(f"inverseOf {term.inverse_of}")
            suffix = f" [{', '.join(traits)}]" if traits else ""
            lines.append(f"rel {term.id}: {domain} -> {target}{suffix}{_gloss(term)}")
    if len(ordered) > len(shown):
        lines.append(
            f'… 另有 {len(ordered) - len(shown)} 个词表项（用 ontology_query mode="schema" 查看）'
        )
    return lines


__all__ = [
    "MAX_ENTAILMENT_ROUNDS",
    "DEFAULT_ENTAILMENT_LIMIT",
    "GraphStats",
    "NeighborResult",
    "all_facts",
    "check_id",
    "class_ancestors",
    "class_descendants",
    "entity_classes",
    "entail",
    "find_path",
    "neighbors",
    "query_entities",
    "query_facts",
    "render_tbox",
    "stats_of",
    "validate_class_input",
    "validate_entity_input",
    "validate_fact_input",
    "validate_relation_input",
]
