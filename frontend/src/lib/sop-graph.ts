/**
 * 功能：SOP 技能卡前端图模型（解析 / 校验 / dagre 布局 / 画布节点与边 / 编辑辅助）。
 * 与后端 bandi/skills/card.py 的 validate_graph 规则保持一致。
 */
import dagre from "dagre";

import type { Edge, Node } from "@xyflow/react";

export const SOP_NODE_WIDTH = 220;
export const SOP_NODE_HEIGHT = 64;

export interface SopNode {
  node_id: string;
  name: string;
  type: string;
  instruction: string;
  optional: boolean;
  condition: string | null;
  expected_user_info: string[];
  allowed_actions: string[];
  knowledge_scope: Record<string, unknown>;
  retry_policy: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

export interface SopEdge {
  source_node_id: string;
  next_node_id: string;
  condition: string | null;
  priority: number;
  label: string | null;
}

export interface SopCard {
  skill_id: string;
  name: string;
  version: string;
  business_domain: string | null;
  description: string;
  enabled: boolean;
  trigger_intents: string[];
  user_utterance_examples: string[];
  goal: string[];
  required_info: string[];
  slot_filling_policy: Record<string, unknown>;
  response_rules: string[];
  interruption_policy: Record<string, string>;
  nodes: SopNode[];
  edges: SopEdge[];
  start_node_id: string;
  terminal_node_ids: string[];
}

export interface SopNodeData extends Record<string, unknown> {
  node: SopNode;
  isStart: boolean;
  isTerminal: boolean;
  error?: boolean;
}

export interface SopPosition {
  x: number;
  y: number;
}

export type SopErrorCode =
  | "empty_nodes"
  | "duplicate_node_id"
  | "missing_start"
  | "empty_terminal"
  | "missing_terminal"
  | "edge_source_missing"
  | "edge_target_missing"
  | "unreachable"
  | "no_exit"
  | "invalid_version";

export interface SopValidationError {
  code: SopErrorCode;
  message: string;
  nodeId?: string;
  edgeIndex?: number;
}

const SEMVER_RE = /^\d+\.\d+\.\d+$/;

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asStringRecord(value: unknown): Record<string, string> {
  const record = asRecord(value);
  return Object.fromEntries(
    Object.entries(record)
      .filter((entry): entry is [string, string] => typeof entry[1] === "string")
      .map(([key, item]) => [key, item]),
  );
}

function normalizeNode(raw: unknown): SopNode {
  const record = asRecord(raw);
  return {
    node_id: asString(record.node_id),
    name: asString(record.name, asString(record.node_id)),
    type: asString(record.type, "collect_info"),
    instruction: asString(record.instruction),
    optional: record.optional === true,
    condition: asNullableString(record.condition),
    expected_user_info: asStringList(record.expected_user_info),
    allowed_actions: asStringList(record.allowed_actions),
    knowledge_scope: asRecord(record.knowledge_scope),
    retry_policy: asRecord(record.retry_policy),
    metadata: asRecord(record.metadata),
  };
}

function normalizeEdge(raw: unknown): SopEdge {
  const record = asRecord(raw);
  return {
    source_node_id: asString(record.source_node_id),
    next_node_id: asString(record.next_node_id),
    condition: asNullableString(record.condition),
    priority: typeof record.priority === "number" ? record.priority : 0,
    label: asNullableString(record.label),
  };
}

/** 功能：解析 SOP 卡 JSON，宽松补齐字段；语法错误统一为可读文案。 */
export function parseSop(content: string): SopCard {
  let raw: unknown;
  try {
    raw = JSON.parse(content);
  } catch (error) {
    throw new Error(`SOP JSON 不合法: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("SOP JSON 不合法: 顶层必须是对象");
  }
  const record = raw as Record<string, unknown>;
  const nodes = Array.isArray(record.nodes) ? record.nodes.map(normalizeNode) : [];
  const edges = Array.isArray(record.edges) ? record.edges.map(normalizeEdge) : [];
  return {
    skill_id: asString(record.skill_id),
    name: asString(record.name, asString(record.skill_id)),
    version: asString(record.version, "1.0.0"),
    business_domain: asNullableString(record.business_domain),
    description: asString(record.description),
    enabled: record.enabled !== false,
    trigger_intents: asStringList(record.trigger_intents),
    user_utterance_examples: asStringList(record.user_utterance_examples),
    goal: asStringList(record.goal),
    required_info: asStringList(record.required_info),
    slot_filling_policy: asRecord(record.slot_filling_policy),
    response_rules: asStringList(record.response_rules),
    interruption_policy: asStringRecord(record.interruption_policy),
    nodes,
    edges,
    start_node_id: asString(record.start_node_id),
    terminal_node_ids: asStringList(record.terminal_node_ids),
  };
}

/** 功能：序列化为后端 PUT /api/resources/sop/{id} 需要的 JSON 字符串。 */
export function serializeSop(card: SopCard): string {
  return JSON.stringify(card, null, 2);
}

function reachableFrom(starts: Set<string>, adjacency: Map<string, string[]>): Set<string> {
  const seen = new Set(starts);
  const queue = [...starts];
  while (queue.length > 0) {
    const current = queue.shift() as string;
    for (const target of adjacency.get(current) ?? []) {
      if (!seen.has(target)) {
        seen.add(target);
        queue.push(target);
      }
    }
  }
  return seen;
}

/** 功能：前端预校验（与后端 validate_graph 同规则），保存前给出可定位错误。 */
export function validateSop(card: SopCard): SopValidationError[] {
  const errors: SopValidationError[] = [];
  if (!SEMVER_RE.test(card.version)) {
    errors.push({ code: "invalid_version", message: `版本必须是三段式 semver，当前为 ${card.version}` });
  }
  if (card.nodes.length === 0) {
    errors.push({ code: "empty_nodes", message: "技能图至少需要 1 个节点" });
    return errors;
  }
  const counts = new Map<string, number>();
  for (const node of card.nodes) {
    counts.set(node.node_id, (counts.get(node.node_id) ?? 0) + 1);
  }
  for (const [nodeId, count] of counts) {
    if (count > 1) {
      errors.push({
        code: "duplicate_node_id",
        message: `node_id 必须唯一: ${nodeId}`,
        nodeId,
      });
    }
  }
  const nodeIds = new Set(card.nodes.map((node) => node.node_id));
  if (!nodeIds.has(card.start_node_id)) {
    errors.push({
      code: "missing_start",
      message: `start_node_id 必须引用存在的节点: ${card.start_node_id || "（空）"}`,
    });
  }
  if (card.terminal_node_ids.length === 0) {
    errors.push({ code: "empty_terminal", message: "terminal_node_ids 至少包含 1 个节点" });
  }
  for (const terminalId of card.terminal_node_ids) {
    if (!nodeIds.has(terminalId)) {
      errors.push({
        code: "missing_terminal",
        message: `terminal_node_ids 引用了缺失节点: ${terminalId}`,
        nodeId: terminalId,
      });
    }
  }
  const outgoing = new Map<string, string[]>();
  const incoming = new Map<string, string[]>();
  for (const nodeId of nodeIds) {
    outgoing.set(nodeId, []);
    incoming.set(nodeId, []);
  }
  card.edges.forEach((edge, index) => {
    if (!nodeIds.has(edge.source_node_id)) {
      errors.push({
        code: "edge_source_missing",
        message: `边 ${index + 1} 的 source 引用缺失节点: ${edge.source_node_id}`,
        edgeIndex: index,
      });
      return;
    }
    if (!nodeIds.has(edge.next_node_id)) {
      errors.push({
        code: "edge_target_missing",
        message: `边 ${index + 1} 的 next 引用缺失节点: ${edge.next_node_id}`,
        edgeIndex: index,
      });
      return;
    }
    outgoing.get(edge.source_node_id)?.push(edge.next_node_id);
    incoming.get(edge.next_node_id)?.push(edge.source_node_id);
  });
  const reachable = reachableFrom(new Set([card.start_node_id]), outgoing);
  for (const nodeId of nodeIds) {
    if (!reachable.has(nodeId)) {
      errors.push({
        code: "unreachable",
        message: `节点 ${nodeId} 从 start 不可达`,
        nodeId,
      });
    }
  }
  const terminals = new Set(card.terminal_node_ids);
  const canExit = reachableFrom(terminals, incoming);
  for (const nodeId of nodeIds) {
    if (!terminals.has(nodeId) && !canExit.has(nodeId)) {
      errors.push({
        code: "no_exit",
        message: `节点 ${nodeId} 无法到达 terminal`,
        nodeId,
      });
    }
  }
  return errors;
}

/** 功能：dagre 纵向布局；每个节点返回左上角坐标。 */
export function layoutSop(card: SopCard): Record<string, SopPosition> {
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 56, marginx: 24, marginy: 20 });
  for (const node of card.nodes) {
    graph.setNode(node.node_id, { width: SOP_NODE_WIDTH, height: SOP_NODE_HEIGHT });
  }
  for (const edge of card.edges) {
    if (graph.hasNode(edge.source_node_id) && graph.hasNode(edge.next_node_id)) {
      graph.setEdge(edge.source_node_id, edge.next_node_id);
    }
  }
  dagre.layout(graph);
  const positions: Record<string, SopPosition> = {};
  card.nodes.forEach((node, index) => {
    const point = graph.node(node.node_id) as { x: number; y: number } | undefined;
    positions[node.node_id] = point
      ? { x: Math.round(point.x - SOP_NODE_WIDTH / 2), y: Math.round(point.y - SOP_NODE_HEIGHT / 2) }
      : { x: 40 + (index % 3) * (SOP_NODE_WIDTH + 40), y: 40 + Math.floor(index / 3) * 100 };
  });
  return positions;
}

/** 功能：读取节点 metadata.position（非法值忽略）。 */
export function readPosition(node: SopNode): SopPosition | undefined {
  const raw = node.metadata?.position;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const point = raw as Record<string, unknown>;
  if (typeof point.x !== "number" || typeof point.y !== "number") return undefined;
  if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) return undefined;
  return { x: point.x, y: point.y };
}

/** 功能：写入节点位置，保留 metadata 的其他键。 */
export function setNodePosition(node: SopNode, position: SopPosition): SopNode {
  return {
    ...node,
    metadata: { ...node.metadata, position: { x: position.x, y: position.y } },
  };
}

/** 功能：生成 ReactFlow 节点（位置优先用已保存坐标，缺失时用 dagre）。 */
export function buildFlowNodes(
  card: SopCard,
  positions: Record<string, SopPosition>,
): Node<SopNodeData>[] {
  const terminals = new Set(card.terminal_node_ids);
  return card.nodes.map((node) => ({
    id: node.node_id,
    type: "sop",
    width: SOP_NODE_WIDTH,
    height: SOP_NODE_HEIGHT,
    position: readPosition(node) ?? positions[node.node_id] ?? { x: 0, y: 0 },
    data: {
      node,
      isStart: node.node_id === card.start_node_id,
      isTerminal: terminals.has(node.node_id),
    },
  }));
}

/** 功能：判断边是否为驳回/返工回退（渲染为虚线）。 */
export function isReworkEdge(edge: SopEdge): boolean {
  return /驳回|返工/.test(`${edge.label ?? ""} ${edge.condition ?? ""}`);
}

/** 功能：生成 ReactFlow 边；标签优先取 label，其次 condition，驳回回退用虚线。 */
export function buildFlowEdges(card: SopCard): Edge[] {
  return card.edges.map((edge, index) => {
    const rework = isReworkEdge(edge);
    return {
      id: `${edge.source_node_id}->${edge.next_node_id}-${index}`,
      source: edge.source_node_id,
      target: edge.next_node_id,
      type: rework ? "rework" : "smoothstep",
      label: edge.label ?? edge.condition ?? undefined,
      data: { edge, index },
      style: rework
        ? { stroke: "#a1a1aa", strokeWidth: 1.4, strokeDasharray: "4 3" }
        : { stroke: "#c7ccd8", strokeWidth: 1.4 },
      labelStyle: { fill: "#6b7280", fontSize: 10 },
      labelBgStyle: { fill: "#f7f8fa", fillOpacity: 0.92 },
    };
  });
}

/** 功能：重命名节点并同步所有边、起点与终点引用。 */
export function renameNode(card: SopCard, oldId: string, newId: string): SopCard {
  const normalized = newId.trim();
  if (!normalized || normalized === oldId) return card;
  if (card.nodes.some((node) => node.node_id === normalized)) {
    throw new Error(`node_id 已存在: ${normalized}`);
  }
  return {
    ...card,
    nodes: card.nodes.map((node) =>
      node.node_id === oldId ? { ...node, node_id: normalized } : node,
    ),
    edges: card.edges.map((edge) => ({
      ...edge,
      source_node_id: edge.source_node_id === oldId ? normalized : edge.source_node_id,
      next_node_id: edge.next_node_id === oldId ? normalized : edge.next_node_id,
    })),
    start_node_id: card.start_node_id === oldId ? normalized : card.start_node_id,
    terminal_node_ids: card.terminal_node_ids.map((nodeId) =>
      nodeId === oldId ? normalized : nodeId,
    ),
  };
}
