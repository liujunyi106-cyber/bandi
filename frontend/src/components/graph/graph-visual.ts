/**
 * 配色：图谱内不用彩色——类型由**首次出现顺序**取 `--graph-gray-1..5` 灰阶
 * （同图稳定、同类同灰、≤5 类零重复）。
 * 标签走 i18n：已知类型用 `graph.type.*`，其余直接显示原类型（动态类无法翻译）。
 * 颜色一律走 CSS 变量引用（不落 token 字面 hex，随 JS 改写）。
 */
import { MIN_NODE_RADIUS } from "@/lib/graph-layout";
import { UNKNOWN_ENTITY_TYPE, type GraphEdge, type GraphEntity } from "@/services/graph";

/** 灰阶色槽位数（`--graph-gray-1` 到 `--graph-gray-5`）。 */
export const GRAY_TOKEN_COUNT = 5;

/** 节点半径梯度档数（连接度越高圆越大，从最大到最小 5 档）。 */
export const RADIUS_TIER_COUNT = 5;

/** 半径梯度系数（最大档 = 布局基准半径，向下递减；基准已含密度自适应）。 */
const RADIUS_TIER_FACTORS: readonly number[] = [1, 0.85, 0.7, 0.55, 0.4];

/** 已知 i18n 标签的类型名（对应 `graph.type.*` 键）；其余动态类名原样显示。 */
const LABELED_TYPES: ReadonlySet<string> = new Set([
  "project",
  "task",
  "note",
  "contact",
  "knowledge",
]);

/** 功能：为当前图的类型分配灰阶（按首次出现顺序，≤5 类零重复）。 */
export function buildTypeColorMap(types: readonly string[]): ReadonlyMap<string, string> {
  const map = new Map<string, string>();
  for (const type of types) {
    if (!type || type === UNKNOWN_ENTITY_TYPE) continue;
    if (map.has(type)) continue;
    map.set(type, `var(--graph-gray-${(map.size % GRAY_TOKEN_COUNT) + 1})`);
  }
  return map;
}

/** 功能：取类型颜色；未知/缺省回落灰（与图例同源）。 */
export function typeColorOf(map: ReadonlyMap<string, string>, type: string): string {
  if (!type || type === UNKNOWN_ENTITY_TYPE) return "var(--muted-foreground)";
  return map.get(type) ?? "var(--muted-foreground)";
}

/**
 * 功能：取类型 i18n key——既有类名走 `graph.type.<type>`；
 * 动态类名原样返回（`t()` 缺 key 时回落 key 本身，即显示类名原名）。
 */
export function entityTypeLabelKey(type: string): string {
  if (!type || type === UNKNOWN_ENTITY_TYPE) return "graph.type.unknown";
  if (LABELED_TYPES.has(type)) return `graph.type.${type}`;
  return type;
}

/** 功能：按字素切分（优先 Intl.Segmenter——emoji ZWJ 组合不拆；无则退化为码点）。 */
function splitGraphemes(value: string): string[] {
  type SegmenterLike = new (
    locales: string | undefined,
    options: { granularity: "grapheme" },
  ) => { segment: (input: string) => Iterable<{ segment: string }> };
  const Segmenter = (Intl as unknown as { Segmenter?: SegmenterLike }).Segmenter;
  if (typeof Segmenter === "function") {
    return Array.from(
      new Segmenter(undefined, { granularity: "grapheme" }).segment(value),
      (entry) => entry.segment,
    );
  }
  return Array.from(value);
}

/** 功能：节点显示名截断（保标签不糊在一起；按字素截断——不拆 emoji 代理对与 ZWJ 组合）。 */
export function truncateLabel(value: string, maxLength = 12): string {
  const trimmed = value.trim();
  const chars = splitGraphemes(trimmed);
  if (chars.length <= maxLength) return trimmed;
  return `${chars.slice(0, maxLength).join("")}…`;
}

/**
 * 功能：按连接度把实体分到 5 档半径（关系越多圆越大，缓解多实体图的无层次混乱）。
 * 最大档 = 布局基准半径（间距不变式由构造成立，不引入重叠）；全部同度时退化为统一半径。
 */
export function buildNodeRadiusMap(
  entities: readonly GraphEntity[],
  edges: readonly GraphEdge[],
  baseRadius: number,
): ReadonlyMap<string, number> {
  const degree = new Map<string, number>();
  for (const entity of entities) degree.set(entity.id, 0);
  for (const edge of edges) {
    if (edge.source === edge.target) continue;
    if (!degree.has(edge.source) || !degree.has(edge.target)) continue;
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }

  const uniformRadius = Math.max(MIN_NODE_RADIUS, baseRadius);
  if (entities.length === 0) return new Map();

  // 唯一度数序列（降序）：度数排名 → 档位（0=最大档）
  const distinctDegrees = [...new Set(degree.values())].sort((left, right) => right - left);
  if (distinctDegrees.length <= 1) {
    return new Map(entities.map((entity) => [entity.id, uniformRadius]));
  }

  const map = new Map<string, number>();
  for (const entity of entities) {
    const rank = distinctDegrees.indexOf(degree.get(entity.id) ?? 0);
    const tier = Math.min(
      RADIUS_TIER_COUNT - 1,
      Math.floor((rank / distinctDegrees.length) * RADIUS_TIER_COUNT),
    );
    map.set(entity.id, Math.max(MIN_NODE_RADIUS, uniformRadius * RADIUS_TIER_FACTORS[tier]));
  }
  return map;
}