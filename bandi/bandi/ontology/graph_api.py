"""本体 API 载荷构造：把三元组图投影成端点可返回的 JSON 形状。

图导出的形状刻意与旧 `/api/entities/graph` 同形
（`{entities:[{id,type,name}], edges:[{source,target,type}]}`），
使前端渲染管线（过滤 → 收敛 → 布局 → SVG）可平移而非重写。

两处与旧实现不同，都是有意的：
- `type` 取实体的**最具体类的展示名**（类的 label 优先、缺省类 id；旧实现取固定枚举，
  动态类下不再适用）——中文项目里类声明带中文 label，图例与卡片即显示中文；
- 截断**显式**（`truncated` + `totalEntities`）；旧实现硬编码 limit=10000 静默截断。
"""

from __future__ import annotations

from typing import Any

from bandi.ontology.engine import all_facts
from bandi.ontology.triple_store import OntologyStore
from bandi.ontology.types import class_of

# 无类实体在图上的回落类型（前端据此给中性色）
UNKNOWN_TYPE = "unknown"


def _class_display(graph: Any, classes: tuple[str, ...]) -> str:
    """功能：实体的展示类型——最具体的类（子类优先），label 优先于 id。

    类名 id 受标识符语法约束（ASCII），而标签承载中文展示名；图例/图谱/卡片
    都按展示名呈现，故此处先取最具体类再回落到其 label。
    """
    if not classes:
        return UNKNOWN_TYPE
    most_specific = sorted(classes)[-1]
    term = class_of(graph, most_specific)
    if term is not None and term.label:
        return term.label
    return most_specific


def stats_payload(store: OntologyStore) -> dict[str, int]:
    """功能：图内计数 + 可推出事实数。"""
    from bandi.ontology.engine import stats_of

    graph = store.graph()
    stats = stats_of(graph)
    inferred = len(all_facts(graph, True)) - stats.facts
    return {
        "classes": stats.classes,
        "relations": stats.relations,
        "entities": stats.entities,
        "facts": stats.facts,
        "inferredFacts": inferred,
    }


def build_graph_payload(
    store: OntologyStore,
    *,
    include_inferred: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """功能：导出图快照——节点取实体，边取实体值的三元组。

    字面量值事实不进图（`objectKind=literal` 的宾语不是节点）。超限时显式标记
    `truncated` 并给出 `totalEntities`，不做静默截断。
    """
    graph = store.graph()
    entities_all = sorted(graph.entities.values(), key=lambda e: e.id)
    page = entities_all[: max(0, limit)]

    node_ids = {entity.id for entity in page}
    entities = [
        {
            "id": entity.id,
            "type": _class_display(graph, entity.classes),
            "name": entity.label or entity.id,
        }
        for entity in page
    ]

    edges: list[dict[str, str]] = []
    for fact in all_facts(graph, include_inferred):
        if fact.object_kind != "entity":
            continue  # 字面量不是节点
        if fact.subject not in node_ids or fact.object not in node_ids:
            continue  # 端点被 limit 截掉时不产出悬空边
        edges.append({"source": fact.subject, "target": fact.object, "type": fact.predicate})

    return {
        "entities": entities,
        "edges": edges,
        "truncated": len(entities_all) > len(page),
        "totalEntities": len(entities_all),
    }


def facts_payload(
    store: OntologyStore,
    *,
    subject: str | None = None,
    predicate: str | None = None,
    object_id: str | None = None,
    include_inferred: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """功能：事实列表载荷——每条带 `via` 标明断言或推理来源。"""
    graph = store.graph()
    matched = [
        fact
        for fact in all_facts(graph, include_inferred)
        if (subject is None or fact.subject == subject)
        and (predicate is None or fact.predicate == predicate)
        and (object_id is None or fact.object == object_id)
    ]
    total = len(matched)
    page = matched[: max(0, limit)]
    facts = [
        {
            "subject": fact.subject,
            "predicate": fact.predicate,
            "object": fact.object,
            "objectKind": fact.object_kind,
            "via": fact.via,
        }
        for fact in page
    ]
    return {
        "facts": facts,
        "truncated": total > len(page),
        "totalFacts": total,
    }


__all__ = [
    "UNKNOWN_TYPE",
    "build_graph_payload",
    "facts_payload",
    "stats_payload",
]
