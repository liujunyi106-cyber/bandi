# 功能：记忆关系读取侧推理——召回命中的结构化三元组按连通分量闭包（只读派生，永不落盘）
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bandi.memory.structured import entailed_structured
from bandi.memory.tbox import relation_for

# 连通分量读取上限（防大库上单次召回扫描变成全量闭包）
DEFAULT_MAX_TRIPLES = 200


def fact_triple_from_metadata(metadata: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """功能：从条目 metadata 提取白名单事实三元组——兼容两种生产形态：

    - `memory_assert` 写入：顶层 subject/predicate/object；
    - 直写结构化：`metadata["structured"]`（kind=fact）。
    """
    meta = dict(metadata or {})
    structured = meta.get("structured")
    if isinstance(structured, dict) and structured.get("kind") == "fact":
        triple = (
            str(structured.get("subject") or "").strip(),
            str(structured.get("predicate") or "").strip(),
            str(structured.get("object") or "").strip(),
        )
        if all(triple) and relation_for(triple[1]) is not None:
            return triple
    triple = (
        str(meta.get("subject") or "").strip(),
        str(meta.get("predicate") or "").strip(),
        str(meta.get("object") or "").strip(),
    )
    if all(triple) and relation_for(triple[1]) is not None:
        return triple
    return None


def collect_related_triples(
    store: Any,
    seeds: Iterable[tuple[str, str, str]],
    *,
    max_triples: int = DEFAULT_MAX_TRIPLES,
) -> list[tuple[str, str, str]]:
    """功能：从长期记忆读与种子三元组连通的三元组——按 subject/object 双向索引
    BFS 扩散（限量）；含种子本身。只读，不写库。"""
    seed_list = [tuple(seed) for seed in seeds]
    collected: set[tuple[str, str, str]] = set(seed_list)
    frontier: set[str] = set()
    for subject, _, obj in seed_list:
        frontier.update((subject, obj))

    by_subject: dict[str, list[tuple[str, str, str]]] = {}
    by_object: dict[str, list[tuple[str, str, str]]] = {}
    for item in store.list_items():
        triple = fact_triple_from_metadata(getattr(item, "metadata", None))
        if triple is None:
            continue
        by_subject.setdefault(triple[0], []).append(triple)
        by_object.setdefault(triple[2], []).append(triple)

    while frontier and len(collected) < max_triples:
        entity = frontier.pop()
        for triple in [*by_subject.get(entity, []), *by_object.get(entity, [])]:
            if triple in collected:
                continue
            collected.add(triple)
            frontier.update((triple[0], triple[2]))
            if len(collected) >= max_triples:
                break
    return list(collected)


def expand_entailed_facts(
    store: Any,
    seeds: Iterable[tuple[str, str, str]],
    *,
    max_triples: int = DEFAULT_MAX_TRIPLES,
) -> list[dict[str, Any]]:
    """功能：读取侧推理入口——种子三元组 → 连通分量 → `entailed_structured`
    闭包 → 派生态列表（带 inferred/derived_via 标记）。"""
    triples = collect_related_triples(store, seeds, max_triples=max_triples)
    entries = [{"kind": "fact", "subject": s, "predicate": p, "object": o} for s, p, o in triples]
    return entailed_structured(entries)


def render_entailed_fact(derived: dict[str, Any]) -> str:
    """功能：派生态渲染为单行事实文本（供命中 metadata 携带）。"""
    return (
        f"{derived.get('subject', '')} {derived.get('predicate', '')} "
        f"{derived.get('object', '')} (via {derived.get('derived_via', '')})"
    )


__all__ = [
    "DEFAULT_MAX_TRIPLES",
    "collect_related_triples",
    "expand_entailed_facts",
    "fact_triple_from_metadata",
    "render_entailed_fact",
]
