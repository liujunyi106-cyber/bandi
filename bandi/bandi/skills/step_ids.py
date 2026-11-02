# 功能：Skill Card 节点/边唯一化与图拓扑保底（node_id 去重、缺省补全
# skill_card_with_unique_step_ids 输入由 SkillCard 改为**模型原始字典**——Bandi
# SkillCard.model_validate 构造即执行唯一性+闭环校验，唯一化必须先于构造）。
from __future__ import annotations

from typing import Any

from bandi.skills.card import SkillCard


def ensure_unique_node_ids(
    nodes: list[Any],
    id_field: str = "node_id",
    label: str = "节点",
) -> tuple[list[dict[str, Any]], list[str]]:
    """功能：节点 id 去重 + 缺省补全，返回规范化节点列表与修正警告。"""
    used: set[str] = set()
    normalized_nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw_node in enumerate(nodes):
        if not isinstance(raw_node, dict):
            continue
        node = dict(raw_node)
        original = str(node.get(id_field) or "").strip()
        base = original or f"node_{index + 1}"
        candidate = base
        suffix = 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        if candidate != original:
            if original:
                warnings.append(f"{label} {index + 1} 的 {id_field} 已修正为 `{candidate}`。")
            else:
                warnings.append(f"{label} {index + 1} 的 {id_field} 已补全为 `{candidate}`。")
            node[id_field] = candidate
        else:
            node[id_field] = original
        used.add(candidate)
        normalized_nodes.append(node)
    return normalized_nodes, warnings


def skill_card_with_unique_step_ids(data: dict[str, Any]) -> tuple[SkillCard, list[str]]:
    """
    返回通过 闭环校验的 SkillCard 与修正警告。
    输入为模型原始字典（含重复 node_id 或死引用时的安全兜底）。"""
    content = {key: value for key, value in data.items()}
    raw_nodes = content.get("nodes") if isinstance(content.get("nodes"), list) else []
    nodes, warnings = ensure_unique_node_ids(raw_nodes)
    id_map: dict[str, str] = {}
    for original, node in zip(
        [item.get("node_id") for item in raw_nodes if isinstance(item, dict)],
        nodes,
        strict=False,
    ):
        if original and node.get("node_id"):
            id_map.setdefault(str(original), str(node.get("node_id") or ""))
    content["nodes"] = nodes
    if id_map:
        content["start_node_id"] = id_map.get(
            str(content.get("start_node_id") or ""), content.get("start_node_id")
        )
        content["terminal_node_ids"] = [
            id_map.get(str(node_id), node_id) for node_id in content.get("terminal_node_ids") or []
        ]
        for edge in content.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            edge["source_node_id"] = id_map.get(
                str(edge.get("source_node_id") or ""), edge.get("source_node_id")
            )
            edge["next_node_id"] = id_map.get(
                str(edge.get("next_node_id") or ""), edge.get("next_node_id")
            )
    _ensure_graph_closure(content, warnings)
    return SkillCard.model_validate(content), warnings


def _ensure_graph_closure(content: dict[str, Any], warnings: list[str]) -> None:
    """功能：图拓扑闭环保底——保证从 start 可达全部节点、每个非终端节点可达某终端。
    通过按节点顺序补线性缺边（仅补「缺前驱」与「缺出口」的最小边）
    重推 start/terminal 到合法值，并同步 set 引用。"""
    nodes = [node for node in content.get("nodes") or [] if isinstance(node, dict)]
    if not nodes:
        return
    node_ids = [str(node.get("node_id") or "") for node in nodes]
    id_set = set(node_ids)
    edges: list[dict[str, Any]] = [
        edge for edge in content.get("edges") or [] if isinstance(edge, dict)
    ]
    existing = {
        (str(edge.get("source_node_id") or ""), str(edge.get("next_node_id") or ""))
        for edge in edges
    }
    incoming: set[str] = {str(edge.get("next_node_id") or "") for edge in edges}

    for index in range(1, len(nodes)):
        target = node_ids[index]
        source = node_ids[index - 1]
        if not target or not source:
            continue
        if target in incoming:
            continue
        pair = (source, target)
        if pair in existing:
            continue
        edges.append(
            {
                "source_node_id": source,
                "next_node_id": target,
                "priority": index,
                "label": "默认推进",
            }
        )
        existing.add(pair)
        incoming.add(target)

    start = str(content.get("start_node_id") or "")
    if start not in id_set:
        if start:
            warnings.append(f"start_node_id 不存在，已改为第一个节点 `{node_ids[0]}`。")
        start = node_ids[0]
        content["start_node_id"] = start

    reachable = _forward_reachable(start, edges)
    if reachable != id_set:
        orphans = sorted(id_set - reachable)
        warnings.append(f"以下节点从 start 不可达，已按节点顺序补链: {', '.join(orphans)}")
        connected = set()
        current = start
        connected.add(current)
        additions: list[dict[str, Any]] = []
        for node_id in node_ids:
            if node_id in connected:
                continue
            additions.append(
                {
                    "source_node_id": current,
                    "next_node_id": node_id,
                    "priority": len(additions),
                    "label": "默认推进",
                }
            )
            connected.add(node_id)
            current = node_id
        edges.extend(additions)

    terminal_ids = [
        str(node_id) for node_id in content.get("terminal_node_ids") or [] if str(node_id) in id_set
    ]
    sink_ids = sorted({node_id for node_id in node_ids if not _outgoing_of(node_id, edges)})
    if sink_ids and set(terminal_ids) != set(sink_ids):
        terminal_ids = sink_ids
        content["terminal_node_ids"] = terminal_ids
    if not terminal_ids:
        terminal_ids = [node_ids[-1]]
        content["terminal_node_ids"] = terminal_ids
    content["edges"] = edges


def _forward_reachable(start: str, edges: list[dict[str, Any]]) -> set[str]:
    """功能：从 start 沿出边 BFS 可达节点集（含 start）。"""
    reachable = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for edge in edges:
            if str(edge.get("source_node_id") or "") == current:
                target = str(edge.get("next_node_id") or "")
                if target and target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
    return reachable


def _outgoing_of(node_id: str, edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """功能：返回以 node_id 为起点的出边列表。"""
    return [edge for edge in edges if str(edge.get("source_node_id") or "") == node_id]
