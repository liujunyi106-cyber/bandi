/**
 * 功能：知识图谱数据层（@base docs/前端设计规划.md §6「知识 · 图谱」）。
 * 后端契约：`GET /api/ontology/graph?includeInferred=&limit=` → `build_graph_payload()`
 * 快照最小字段 `{entities:[{id,type,name}], edges:[{source,target,type}]}`（无坐标，布局由前端算）。
 * 实体类型 = 用户声明的类名（动态无上限）——颜色/标签由 graph-visual 按类名散列与回退。
 * 本层职责：拉取 + **归一化**（脏数据防御）+ **统计**（图例/空态/默认聚焦用）。
 */
import { api } from "@/services/api";

/** 无类实体的兜底类型（与后端 `graph_api.UNKNOWN_TYPE` 同值）。 */
export const UNKNOWN_ENTITY_TYPE = "unknown";

export interface GraphEntity {
  id: string;
  /** 实体最具体类名（用户声明，动态）；无类时为 `unknown`。 */
  type: string;
  name: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  /** 关系类型（`belongs_to` / `depends_on` / `references` …）；缺失为空串。 */
  type: string;
}

export interface GraphSnapshot {
  entities: GraphEntity[];
  edges: GraphEdge[];
}

export interface GraphSummary {
  entityCount: number;
  edgeCount: number;
  /** 每个**实际出现**的类 → 计数（动态；图例按此渲染，不再预置固定五类）。 */
  typeCounts: Record<string, number>;
  /** 度数为 0 的实体 id（顺序跟随实体表）。 */
  orphanEntityIds: string[];
  /** 度数最高的前 5 个实体（默认聚焦 / 空态提示用）。 */
  hubs: { id: string; degree: number }[];
}

const EMPTY_SNAPSHOT: GraphSnapshot = { entities: [], edges: [] };

const HUB_LIMIT = 5;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readNonEmptyString(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** 功能：归一化实体表——丢无 id、id 去重保首个、类型原样保留（动态类不设白名单）、name 缺省回落 id。 */
function normalizeEntities(raw: unknown): GraphEntity[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const entities: GraphEntity[] = [];

  for (const item of raw) {
    if (!isRecord(item)) continue;
    const id = readNonEmptyString(item.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);

    const type = readNonEmptyString(item.type) ?? UNKNOWN_ENTITY_TYPE;

    entities.push({ id, type, name: readNonEmptyString(item.name) ?? id });
  }
  return entities;
}

/** 功能：归一化边表——丢悬空/自环/重复，字段缺失容错。 */
function normalizeEdges(raw: unknown, entityIds: ReadonlySet<string>): GraphEdge[] {
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const edges: GraphEdge[] = [];

  for (const item of raw) {
    if (!isRecord(item)) continue;
    const source = readNonEmptyString(item.source);
    const target = readNonEmptyString(item.target);
    if (!source || !target) continue;
    if (source === target) continue;
    if (!entityIds.has(source) || !entityIds.has(target)) continue;

    const type = readNonEmptyString(item.type) ?? "";
    const key = `${source}\u0000${target}\u0000${type}`;
    if (seen.has(key)) continue;
    seen.add(key);
    edges.push({ source, target, type });
  }
  return edges;
}

/** 功能：把任意后端响应归一化为可靠快照（非对象输入退化为空快照，绝不抛错）。 */
export function normalizeSnapshot(raw: unknown): GraphSnapshot {
  if (!isRecord(raw)) return { ...EMPTY_SNAPSHOT };
  const entities = normalizeEntities(raw.entities);
  const entityIds = new Set(entities.map((entity) => entity.id));
  return { entities, edges: normalizeEdges(raw.edges, entityIds) };
}

/** 功能：全量度数表（**不得用 describeGraph().hubs** —— 那里只留前 5 名）。 */
function computeDegrees(snapshot: GraphSnapshot): Map<string, number> {
  const degree = new Map<string, number>(snapshot.entities.map((entity) => [entity.id, 0]));
  for (const edge of snapshot.edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  return degree;
}

/** 功能：图统计（图例计数 / 孤立点 / 枢纽），供页面空态与默认聚焦使用。 */
export function describeGraph(snapshot: GraphSnapshot): GraphSummary {
  const typeCounts: Record<string, number> = {};
  for (const entity of snapshot.entities) {
    typeCounts[entity.type] = (typeCounts[entity.type] ?? 0) + 1;
  }

  const degree = computeDegrees(snapshot);

  const orphanEntityIds = snapshot.entities
    .filter((entity) => (degree.get(entity.id) ?? 0) === 0)
    .map((entity) => entity.id);

  const hubs = [...degree.entries()]
    .filter(([, value]) => value > 0)
    .sort((left, right) => right[1] - left[1])
    .slice(0, HUB_LIMIT)
    .map(([id, value]) => ({ id, degree: value }));

  return {
    entityCount: snapshot.entities.length,
    edgeCount: snapshot.edges.length,
    typeCounts,
    orphanEntityIds,
    hubs,
  };
}

export interface FetchGraphOptions {
  /** 是否包含推理边（后端默认 false；开启后镜像/传递边会进图）。 */
  includeInferred?: boolean;
  signal?: AbortSignal;
}

/** SVG 渲染实体上限：超过则按度数取前 N 渲染（防浏览器被上万节点拖死）。 */
export const MAX_RENDER_ENTITIES = 800;

export interface FilterResult {
  /** 过滤后的子图（命中实体 + 其一跳邻域，两端均保留的边）。 */
  snapshot: GraphSnapshot;
  /** 被过滤掉的实体数（UI 需如实告知）。 */
  hiddenEntityCount: number;
  /** 因端点被过滤而一并隐藏的边数。 */
  hiddenEdgeCount: number;
}

/**
 * 功能：按名称子串过滤快照（大小写不敏感、trim 后空查询原样返回）。
 * 语义拍板：命中实体**保留其一跳邻域**——只显示命中点会让图碎成孤岛，无法继续探索；
 * 边仅保留两端都在保留集内的。确定性：实体/边保持原快照相对顺序。
 */
export function filterSnapshot(snapshot: GraphSnapshot, rawQuery: string): FilterResult {
  const query = rawQuery.trim().toLocaleLowerCase();
  if (query === "") {
    return { snapshot, hiddenEntityCount: 0, hiddenEdgeCount: 0 };
  }

  const keep = new Set<string>();
  for (const entity of snapshot.entities) {
    if (entity.name.toLocaleLowerCase().includes(query)) {
      keep.add(entity.id);
      for (const edge of snapshot.edges) {
        if (edge.source === entity.id) keep.add(edge.target);
        if (edge.target === entity.id) keep.add(edge.source);
      }
    }
  }

  const entities = snapshot.entities.filter((entity) => keep.has(entity.id));
  const allEdges = snapshot.edges;
  const edges = allEdges.filter((edge) => keep.has(edge.source) && keep.has(edge.target));

  return {
    snapshot: { entities, edges },
    hiddenEntityCount: snapshot.entities.length - entities.length,
    hiddenEdgeCount: allEdges.length - edges.length,
  };
}

export interface RenderableSubgraph {
  snapshot: GraphSnapshot;
  /** 被折叠掉的实体数（UI 需如实告知）。 */
  hiddenEntityCount: number;
  /** 因端点被折叠而一并隐藏的边数。 */
  hiddenEdgeCount: number;
  /** 是否发生了折叠。 */
  truncated: boolean;
}

/**
 * 功能：把快照收敛到可渲染规模——按度数降序取前 limit 个实体（度数并列按实体表顺序
 * 保证确定性），两端都被保留的边才留下。未超限时原样返回（零拷贝成本）。
 */
export function selectRenderableSubgraph(
  snapshot: GraphSnapshot,
  limit = MAX_RENDER_ENTITIES,
): RenderableSubgraph {
  if (snapshot.entities.length <= limit) {
    return { snapshot, hiddenEntityCount: 0, hiddenEdgeCount: 0, truncated: false };
  }

  const degree = computeDegrees(snapshot);
  const order = new Map(snapshot.entities.map((entity, index) => [entity.id, index]));

  const kept = [...snapshot.entities]
    .sort((left, right) => {
      const degreeDiff = (degree.get(right.id) ?? 0) - (degree.get(left.id) ?? 0);
      if (degreeDiff !== 0) return degreeDiff;
      return (order.get(left.id) ?? 0) - (order.get(right.id) ?? 0);
    })
    .slice(0, limit);

  const keptIds = new Set(kept.map((entity) => entity.id));
  const edges = snapshot.edges.filter(
    (edge) => keptIds.has(edge.source) && keptIds.has(edge.target),
  );

  return {
    snapshot: { entities: kept, edges },
    hiddenEntityCount: snapshot.entities.length - kept.length,
    hiddenEdgeCount: snapshot.edges.length - edges.length,
    truncated: true,
  };
}

/** 功能：拉取本体图谱快照（同源 `/api`，经 vite 代理到内核）。 */
export async function fetchGraph(options: FetchGraphOptions = {}): Promise<GraphSnapshot> {
  const params = new URLSearchParams();
  if (options.includeInferred) params.set("includeInferred", "true");
  const query = params.toString();

  const raw = await api.get<unknown>(`/ontology/graph${query ? `?${query}` : ""}`, {
    signal: options.signal,
  });
  return normalizeSnapshot(raw);
}
