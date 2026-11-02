"""本体四工具：define / assert / query / retract（复刻 dsh-ontology `src/tools.ts`，727 行）。

面向模型的四个工具，每个返回规范 JSON 值（便于程序化消费），人读解释只放在
`render()` 里。

关键机制：
- **define 用反复遍解析**：同一次调用里的定义按「反复重试直到无进展」应用，故子类
  可先于父类声明、关系可先于其逆；只有永远无法解析的才被拒。
- **assert 先建实体后写事实**：一次调用可同时引入个体及其关系。
- **query 一个工具六个模式**：让模型可见的工具面保持小，而图的每种访问方式
  （查找/遍历/连通性/模式检视）都可达。
- **retract 撤回词表被依赖时拒绝**，并点名依赖者。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from bandi.ontology.engine import (
    all_facts,
    class_ancestors,
    find_path,
    neighbors,
    query_entities,
    query_facts,
    render_tbox,
    stats_of,
)
from bandi.ontology.triple_store import OntologyStore
from bandi.ontology.types import DerivedFact, Violation

# 六模式（对照表 -3）
QUERY_MODES = ("schema", "stats", "entities", "facts", "neighbors", "path")
# 邻域/路径的深度钳制区间与默认值
MIN_DEPTH = 1
MAX_DEPTH = 8
DEFAULT_DEPTH = 2


@dataclass
class OntologyToolContext:
    """功能：工具读取的运行时旋钮（store / 推论开关 / 结果规模 / 时钟）。"""

    store: OntologyStore
    inference: bool = True
    default_limit: int = 20
    max_limit: int = 100
    now: Callable[[], str] = lambda: datetime.now(UTC).isoformat()


@dataclass
class OntologyTool:
    """功能：一个工具定义——名字、描述、参数 schema、输出 schema、执行与渲染。"""

    name: str
    description: str
    parameters: dict[str, Any]
    output_schema: dict[str, Any]
    execute: Callable[[dict], dict]
    render: Callable[[dict], str]


def _messages(violations: list[Violation]) -> list[str]:
    """功能：违规压平为文案列表（工具结果携带的形式）。"""
    return [v.message for v in violations]


def _project_fact(fact: DerivedFact) -> dict[str, Any]:
    """功能：事实投影为规范形状——缺省可选字段不出现，保持 JSON 无损。"""
    out: dict[str, Any] = {
        "subject": fact.subject,
        "predicate": fact.predicate,
        "object": fact.object,
        "objectKind": fact.object_kind,
        "via": fact.via,
    }
    if fact.source is not None:
        out["source"] = fact.source
    if fact.confidence is not None:
        out["confidence"] = fact.confidence
    if fact.violations:
        out["violations"] = list(fact.violations)
    return out


def _fact_line(fact: dict[str, Any]) -> str:
    """功能：事实渲染成一行散文。"""
    obj = (
        json.dumps(fact["object"], ensure_ascii=False)
        if fact["objectKind"] == "literal"
        else fact["object"]
    )
    via = "" if fact["via"] == "asserted" else f"（推论自 {fact['via']}）"
    violations = fact.get("violations") or []
    flags = "" if not violations else f" ⚠ {'; '.join(violations)}"
    return f"{fact['subject']} {fact['predicate']} {obj}{via}{flags}"


def _resolve_limit(requested: Any, context: OntologyToolContext) -> int:
    """功能：把请求的结果规模钳进配置区间。"""
    if requested is None:
        return context.default_limit
    return max(1, min(int(requested), context.max_limit))


def _resolve_depth(requested: Any) -> int:
    """功能：深度钳制到 [1, 8]，缺省 2。"""
    return max(
        MIN_DEPTH, min(int(requested) if requested is not None else DEFAULT_DEPTH, MAX_DEPTH)
    )


# 工具入参用驼峰，引擎与存储用 snake_case——在此归一。
_CAMEL_TO_SNAKE = {
    "subClassOf": "sub_class_of",
    "rangeKind": "range_kind",
    "inverseOf": "inverse_of",
}


def _normalize_input(item: dict) -> dict:
    """功能：把驼峰入参键名归一为引擎期望的 snake_case（未知键原样保留）。"""
    return {_CAMEL_TO_SNAKE.get(key, key): value for key, value in item.items()}


# ── ontology_define ─────────────────────────────────────────────────────

_DEFINE_DESCRIPTION = (
    "声明本体词汇（TBox）：类以及类之间的有类型关系。断言事实前先做这一步——"
    "ontology_assert 会拒绝未声明的谓词。关系有域（主语类）、值域（宾语类；"
    '属性值如日期、版本号这类用 rangeKind="literal"），以及可选特征让图能'
    '推出你从未断言过的事实："transitive"（a part_of b、b part_of c ⇒ a part_of c）、'
    '"symmetric"（双向成立）、"functional"（每个主语至多一个宾语，第二个会被当作冲突拒绝）。'
    '"inverseOf" 把一个关系与它的反向配对（owns / owned_by）。顺序无关：定义按反复重试解析。'
    "重定义既有词表项是整体替换。"
)


def build_define_tool(context: OntologyToolContext) -> OntologyTool:
    """功能：构建 `ontology_define` 工具——声明图可用的词汇。"""

    def execute(args: dict) -> dict:
        now = context.now()
        defined_classes: list[str] = []
        defined_relations: list[str] = []
        rejected: list[dict[str, Any]] = []

        pending: list[tuple[str, dict]] = [
            *(("class", item) for item in (args.get("classes") or [])),
            *(("relation", item) for item in (args.get("relations") or [])),
        ]
        # 反复遍：仅因「引用的词表项在同一次调用里排在后面」而失败的定义，
        # 下一遍就会成功；一整遍毫无进展即意味着剩下的永远解析不了。
        while pending:
            still_pending: list[tuple[str, dict]] = []
            failures: dict[str, list[str]] = {}
            for kind, item in pending:
                payload = _normalize_input(item)
                if kind == "class":
                    outcome = context.store.define_class(payload, now)
                else:
                    outcome = context.store.define_relation(payload, now)
                if outcome.ok:
                    (defined_classes if kind == "class" else defined_relations).append(item["id"])
                else:
                    still_pending.append((kind, item))
                    failures[item["id"]] = _messages(outcome.violations)
            if len(still_pending) == len(pending):
                for _, item in still_pending:
                    rejected.append(
                        {
                            "ref": item["id"],
                            "violations": failures.get(item["id"], ["已被拒绝"]),
                        }
                    )
                break
            pending = still_pending

        return {"classes": defined_classes, "relations": defined_relations, "rejected": rejected}

    def render(value: dict) -> str:
        lines = [f"已定义 {len(value['classes'])} 个类、{len(value['relations'])} 个关系"]
        if value["classes"]:
            lines.append(f"类：{', '.join(value['classes'])}")
        if value["relations"]:
            lines.append(f"关系：{', '.join(value['relations'])}")
        for rejection in value["rejected"]:
            lines.append(f"已拒绝 {rejection['ref']}：{'; '.join(rejection['violations'])}")
        return "\n".join(lines)

    return OntologyTool(
        name="ontology_define",
        description=_DEFINE_DESCRIPTION,
        parameters={
            "classes": {
                "type": "array",
                "description": "要声明的类",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "required": True,
                            "description": "标识符，如 Service 或 schema:Person",
                        },
                        "label": {"type": "string", "description": "人读名称"},
                        "comment": {"type": "string", "description": "归入该类意味着什么"},
                        "subClassOf": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "直接父类；归属向上传递",
                        },
                    },
                },
            },
            "relations": {
                "type": "array",
                "description": "要声明的关系",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "required": True,
                            "description": "标识符，如 depends_on",
                        },
                        "label": {"type": "string"},
                        "comment": {"type": "string", "description": "断言该关系主张什么"},
                        "domain": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "主语必须归属的类；省略表示不加约束",
                        },
                        "rangeKind": {
                            "type": "string",
                            "enum": ["entity", "literal"],
                            "description": "宾语是实体（默认）还是字面量",
                        },
                        "range": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "实体宾语必须归属的类；rangeKind 为 literal 时无效",
                        },
                        "characteristics": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["transitive", "symmetric", "functional"],
                            },
                        },
                        "inverseOf": {"type": "string", "description": "反向成立的关系"},
                    },
                },
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "classes": {"type": "array", "required": True, "items": {"type": "string"}},
                "relations": {"type": "array", "required": True, "items": {"type": "string"}},
                "rejected": {"type": "array", "required": True, "items": {"type": "object"}},
            },
        },
        execute=execute,
        render=render,
    )


# ── ontology_assert ─────────────────────────────────────────────────────

_ASSERT_DESCRIPTION = (
    "把实体与事实记进本体。同一次调用里的实体先于其事实建立，故一次调用即可"
    "同时引入一个个体和它的关系。每条事实都对照已声明词汇校验：谓词须存在、"
    "主语须落在它的域内、宾语须是值域内的已知实体（字面量值关系则是字面量）。"
    "被拒的事实会明确告诉你是哪条约束没过——修输入或用 ontology_define 扩充词汇，"
    "不要绕过。重复断言同一三元组是幂等的；实体的类跨调用累积。"
    "实体必须与其关系成对断言：只引入实体不引入事实会触发孤立实体确认"
    "（无确认通道时直接回滚），本体不允许孤立实体。"
)


def _entity_degree(graph: Any, entity_id: str) -> int:
    """功能：实体在断言事实里的度数（主语或实体型宾语各计一次；字面量宾语不计）。"""
    degree = 0
    for fact in graph.facts.values():
        if fact.subject == entity_id:
            degree += 1
        elif fact.object_kind == "entity" and fact.object == entity_id:
            degree += 1
    return degree


def _orphan_entities(
    context: OntologyToolContext, asserted_entities: list[str], preexisting: set[str]
) -> list[str]:
    """功能：本轮新断言且收口后零度数的实体（孤立实体候选，本体合法性检查）。"""
    if not asserted_entities:
        return []
    graph = context.store.graph()
    return [
        entity_id
        for entity_id in asserted_entities
        if entity_id not in preexisting and _entity_degree(graph, entity_id) == 0
    ]


def build_assert_tool(context: OntologyToolContext) -> OntologyTool:
    """功能：构建 `ontology_assert` 工具——把实例数据记进 TBox 约束之下。"""

    def execute(args: dict) -> dict:
        now = context.now()
        asserted_entities: list[str] = []
        asserted_facts: list[str] = []
        rejected: list[dict[str, Any]] = []
        warnings: list[str] = []
        # 本轮新增口径：调用前已存在的实体不算新断言（零度也不算孤立）
        preexisting = set(context.store.graph().entities)

        for entity in args.get("entities") or []:
            outcome = context.store.assert_entity(entity, now)
            if outcome.ok:
                asserted_entities.append(entity["id"])
            else:
                rejected.append({"ref": entity["id"], "violations": _messages(outcome.violations)})
        for fact in args.get("facts") or []:
            ref = f"{fact['subject']} {fact['predicate']} {fact['object']}"
            outcome = context.store.assert_fact(fact, now)
            if not outcome.ok:
                rejected.append({"ref": ref, "violations": _messages(outcome.violations)})
                continue
            asserted_facts.append(ref)
            if outcome.violations:
                # 非 strict 模式照样落了库——把不一致显式surfaced 出来
                warnings.append(f"{ref}：{'; '.join(_messages(outcome.violations))}")

        return {
            "entities": asserted_entities,
            "facts": asserted_facts,
            "rejected": rejected,
            "warnings": warnings,
            "orphan_entities": _orphan_entities(context, asserted_entities, preexisting),
        }

    def render(value: dict) -> str:
        lines = [f"已断言 {len(value['entities'])} 个实体、{len(value['facts'])} 条事实"]
        for warning in value["warnings"]:
            lines.append(f"警告 {warning}")
        for rejection in value["rejected"]:
            lines.append(f"已拒绝 {rejection['ref']}：{'; '.join(rejection['violations'])}")
        for entity_id in value.get("confirmed_orphans") or []:
            lines.append(f"孤立实体 {entity_id} 已确认保留（无关系独立存在）")
        for entity_id in value.get("rolled_back_entities") or []:
            lines.append(
                f"已回滚孤立实体 {entity_id}（未建立任何关系）——请将实体与事实成对断言后重试"
            )
        return "\n".join(lines)

    return OntologyTool(
        name="ontology_assert",
        description=_ASSERT_DESCRIPTION,
        parameters={
            "entities": {
                "type": "array",
                "description": "要记录的个体",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "required": True},
                        "classes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "声明的类；每个都必须已存在",
                        },
                        "label": {"type": "string"},
                        "comment": {"type": "string", "description": "关于该个体已知什么"},
                    },
                },
            },
            "facts": {
                "type": "array",
                "description": "要记录的三元组",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "required": True, "description": "实体 id"},
                        "predicate": {
                            "type": "string",
                            "required": True,
                            "description": "已声明的关系 id",
                        },
                        "object": {
                            "type": "string",
                            "required": True,
                            "description": "实体 id；字面量值关系则是字面量",
                        },
                        "source": {"type": "string", "description": "该主张从何而来"},
                        "confidence": {"type": "number", "description": "[0,1] 的置信度"},
                    },
                },
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "entities": {"type": "array", "required": True, "items": {"type": "string"}},
                "facts": {"type": "array", "required": True, "items": {"type": "string"}},
                "rejected": {"type": "array", "required": True, "items": {"type": "object"}},
                "warnings": {"type": "array", "required": True, "items": {"type": "string"}},
                "orphan_entities": {
                    "type": "array",
                    "required": True,
                    "items": {"type": "string"},
                    "description": "本轮新断言且收口后零关系（孤立）的实体",
                },
            },
        },
        execute=execute,
        render=render,
    )


# ── ontology_query ──────────────────────────────────────────────────────

_QUERY_DESCRIPTION = (
    '读本体。各模式："schema" 列出已声明词汇（不知道有什么时从这里开始）；'
    '"stats" 统计一切；"entities" 按类（含子类）与/或文本匹配找个体；'
    '"facts" 匹配三元组模式，省略的位置是通配；"neighbors" 从某个实体按有限'
    '深度向外扩展；"path" 找连接两个实体的最短关系链。置 includeInferred 可'
    "额外得到由传递/对称/逆关系推出的事实——它们是读时推导的，并标明获得方式。"
)


def build_query_tool(context: OntologyToolContext) -> OntologyTool:
    """功能：构建 `ontology_query` 工具——进图的唯一读路径。"""

    def execute(args: dict) -> dict:
        graph = context.store.graph()
        include_inferred = context.inference and bool(args.get("includeInferred", False))
        limit = _resolve_limit(args.get("limit"), context)
        depth = _resolve_depth(args.get("depth"))
        mode = args.get("mode")

        if mode == "stats":
            stats = stats_of(graph)
            inferred = len(all_facts(graph, True)) - stats.facts if context.inference else 0
            return {
                "mode": "stats",
                "stats": {
                    "classes": stats.classes,
                    "relations": stats.relations,
                    "entities": stats.entities,
                    "facts": stats.facts,
                    "inferredFacts": inferred,
                },
            }
        if mode == "schema":
            return {"mode": "schema", "schema": render_tbox(graph, limit)}
        if mode == "entities":
            found = query_entities(
                graph,
                {
                    **({"class_id": args["class"]} if args.get("class") else {}),
                    **({"search": args["search"]} if args.get("search") else {}),
                    "limit": limit + 1,
                },
            )
            page = found[:limit]
            return {
                "mode": "entities",
                "entities": [
                    {
                        "id": entity.id,
                        "classes": list(entity.classes),
                        "allClasses": sorted(
                            {
                                ancestor
                                for direct in entity.classes
                                for ancestor in class_ancestors(graph.terms, direct)
                            }
                        ),
                        **({"label": entity.label} if entity.label is not None else {}),
                        **({"comment": entity.comment} if entity.comment is not None else {}),
                    }
                    for entity in page
                ],
                "truncated": len(found) > len(page),
            }
        if mode == "facts":
            found = query_facts(
                graph,
                {
                    **({"subject": args["subject"]} if args.get("subject") else {}),
                    **({"predicate": args["predicate"]} if args.get("predicate") else {}),
                    **({"object": args["object"]} if args.get("object") else {}),
                    "include_inferred": include_inferred,
                    "limit": limit + 1,
                },
            )
            page = found[:limit]
            return {
                "mode": "facts",
                "facts": [_project_fact(f) for f in page],
                "truncated": len(found) > len(page),
            }
        if mode == "neighbors":
            if not args.get("entity"):
                raise ValueError('neighbors 模式需要 "entity"')
            result = neighbors(graph, args["entity"], depth, include_inferred)
            page = result.facts[:limit]
            return {
                "mode": "neighbors",
                "reached": result.reached,
                "facts": [_project_fact(f) for f in page],
                "truncated": len(result.facts) > len(page),
            }
        if mode == "path":
            if not args.get("from") or not args.get("to"):
                raise ValueError('path 模式需要 "from" 与 "to"')
            path = find_path(graph, args["from"], args["to"], depth, include_inferred)
            return {
                "mode": "path",
                "connected": path is not None,
                "path": [_project_fact(f) for f in (path or [])],
            }
        raise ValueError(f"不支持的模式 {mode!r}")

    def render(value: dict) -> str:
        lines: list[str] = []
        mode = value.get("mode")
        if mode == "stats":
            stats = value["stats"]
            lines.append(
                f"{stats['classes']} 个类、{stats['relations']} 个关系、"
                f"{stats['entities']} 个实体、{stats['facts']} 条断言事实"
                + (f"、{stats['inferredFacts']} 条可推出" if stats["inferredFacts"] > 0 else "")
            )
        elif mode == "schema":
            lines.extend(value.get("schema") or [])
            if not lines:
                lines.append("本体尚无词汇——先定义类与关系。")
        elif mode == "entities":
            for entity in value.get("entities") or []:
                classes = f" : {', '.join(entity['classes'])}" if entity["classes"] else ""
                gloss = entity.get("comment") or entity.get("label")
                lines.append(f"{entity['id']}{classes}" + (f"  — {gloss}" if gloss else ""))
            if not (value.get("entities") or []):
                lines.append("没有匹配的实体")
        elif mode == "facts":
            lines.extend(_fact_line(f) for f in value.get("facts") or [])
            if not (value.get("facts") or []):
                lines.append("没有匹配的事实")
        elif mode == "neighbors":
            lines.append(f"到达 {len(value.get('reached') or [])} 个实体")
            lines.extend(_fact_line(f) for f in value.get("facts") or [])
        elif mode == "path":
            if value.get("connected"):
                lines.append(f"连通，共 {len(value.get('path') or [])} 步：")
                lines.extend(f"  {_fact_line(f)}" for f in value.get("path") or [])
            else:
                lines.append("给定深度内无连通路径")
        if value.get("truncated") is True:
            lines.append("… 已截断；请收窄查询或提高 limit")
        return "\n".join(lines)

    return OntologyTool(
        name="ontology_query",
        description=_QUERY_DESCRIPTION,
        parameters={
            "mode": {"type": "string", "required": True, "enum": list(QUERY_MODES)},
            "class": {"type": "string", "description": "entities 模式：限定该类及其子类"},
            "search": {"type": "string", "description": "entities 模式：id/label/comment 子串"},
            "subject": {"type": "string", "description": "facts 模式：匹配该主语"},
            "predicate": {"type": "string", "description": "facts 模式：匹配该关系"},
            "object": {"type": "string", "description": "facts 模式：匹配该宾语"},
            "entity": {"type": "string", "description": "neighbors 模式：扩展起点"},
            "from": {"type": "string", "description": "path 模式：起点实体"},
            "to": {"type": "string", "description": "path 模式：终点实体"},
            "depth": {"type": "integer", "description": "neighbors/path 模式：最大边数（默认 2）"},
            "includeInferred": {"type": "boolean", "description": "含推出事实（默认 false）"},
            "limit": {"type": "integer", "description": "结果上限"},
        },
        output_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "required": True},
                "stats": {"type": "object"},
                "schema": {"type": "array", "items": {"type": "string"}},
                "entities": {"type": "array", "items": {"type": "object"}},
                "facts": {"type": "array", "items": {"type": "object"}},
                "path": {"type": "array", "items": {"type": "object"}},
                "connected": {"type": "boolean"},
                "reached": {"type": "array", "items": {"type": "string"}},
                "truncated": {"type": "boolean"},
            },
        },
        execute=execute,
        render=render,
    )


# ── ontology_retract ────────────────────────────────────────────────────

_RETRACT_DESCRIPTION = (
    "从本体里移除东西。撤回实体会连带移除所有提及它的事实。撤回词表项在仍被依赖时"
    "会被拒绝（仍为实体归类的类、出现在关系签名里的类、仍被断言的关系）——先清掉依赖者。"
    "推出事实没有独立存在：撤回一个前提，由它派生的一切随之消失。"
)


def build_retract_tool(context: OntologyToolContext) -> OntologyTool:
    """功能：构建 `ontology_retract` 工具——唯一会删除的写路径。"""

    def execute(args: dict) -> dict:
        terms: list[str] = []
        entities: list[str] = []
        facts: list[str] = []
        refused: list[dict[str, Any]] = []
        missing: list[str] = []
        cascaded = 0

        for term_id in args.get("terms") or []:
            outcome = context.store.retract_term(term_id)
            if not outcome.ok:
                refused.append({"ref": term_id, "violations": _messages(outcome.violations)})
            elif outcome.removed:
                terms.append(term_id)
            else:
                missing.append(term_id)
        for entity_id in args.get("entities") or []:
            outcome = context.store.retract_entity(entity_id)
            cascaded += outcome.facts_removed
            if outcome.removed:
                entities.append(entity_id)
            else:
                missing.append(entity_id)
        for fact in args.get("facts") or []:
            ref = f"{fact['subject']} {fact['predicate']} {fact['object']}"
            if context.store.retract_fact(fact["subject"], fact["predicate"], fact["object"]):
                facts.append(ref)
            else:
                missing.append(ref)

        return {
            "terms": terms,
            "entities": entities,
            "facts": facts,
            "cascadedFacts": cascaded,
            "refused": refused,
            "missing": missing,
        }

    def render(value: dict) -> str:
        lines = [
            f"已撤回 {len(value['terms'])} 个词表项、{len(value['entities'])} 个实体、"
            f"{len(value['facts'])} 条事实"
            + (
                f"（另有 {value['cascadedFacts']} 条级联删除）"
                if value["cascadedFacts"] > 0
                else ""
            )
        ]
        for refusal in value["refused"]:
            lines.append(f"已拒绝 {refusal['ref']}：{'; '.join(refusal['violations'])}")
        if value["missing"]:
            lines.append(f"不存在：{', '.join(value['missing'])}")
        return "\n".join(lines)

    return OntologyTool(
        name="ontology_retract",
        description=_RETRACT_DESCRIPTION,
        parameters={
            "terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要从词汇中移除的类或关系 id",
            },
            "entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要移除的实体 id",
            },
            "facts": {
                "type": "array",
                "description": "要移除的单个三元组",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {"type": "string", "required": True},
                        "predicate": {"type": "string", "required": True},
                        "object": {"type": "string", "required": True},
                    },
                },
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "terms": {"type": "array", "required": True, "items": {"type": "string"}},
                "entities": {"type": "array", "required": True, "items": {"type": "string"}},
                "facts": {"type": "array", "required": True, "items": {"type": "string"}},
                "cascadedFacts": {"type": "integer", "required": True},
                "refused": {"type": "array", "required": True, "items": {"type": "object"}},
                "missing": {"type": "array", "required": True, "items": {"type": "string"}},
            },
        },
        execute=execute,
        render=render,
    )


def create_ontology_tools(context: OntologyToolContext) -> list[OntologyTool]:
    """功能：构建全部本体工具（按注册顺序）。"""
    return [
        build_define_tool(context),
        build_assert_tool(context),
        build_query_tool(context),
        build_retract_tool(context),
    ]


__all__ = [
    "DEFAULT_DEPTH",
    "MAX_DEPTH",
    "MIN_DEPTH",
    "QUERY_MODES",
    "OntologyTool",
    "OntologyToolContext",
    "build_assert_tool",
    "build_define_tool",
    "build_query_tool",
    "build_retract_tool",
    "create_ontology_tools",
]
