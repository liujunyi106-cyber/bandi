/**
 * 功能：图谱力导向布局层（**可替换纯函数**：只吃实体/边，吐坐标；视图层不碰算法）。
 * 实现选型 = d3-force（docs/ADR.md 决策：Barnes-Hut O(n log n) + 成熟参数
 * 因后端全图上限 10000 实体、且 要「过滤后重排」的交互式探索）。
 * 确定性：种子化随机源 + 手算初始摆位 + 固定 ticks（`stop()` 后手动 tick，无 rAF/定时器）
 * → 同输入必得同布局，可写坐标级断言。
 * 「知识 · 图谱」；技法对齐 datafoundry `TraceDagCanvas`。
 */
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  type Force,
} from "d3-force";

import type { Bounds } from "@/lib/graph-view";
import type { GraphEdge, GraphEntity } from "@/services/graph";

/** 节点圆点半径（命中半径另算，见 GraphCanvas）。 */
export const NODE_RADIUS = 13;

/** 节点半径下限：超密时允许缩到这么小，仍靠独立命中半径保证可点选。 */
export const MIN_NODE_RADIUS = 3;

/** 命中半径下限：与渲染半径解耦，小圆点也要好点中。 */
export const MIN_HIT_RADIUS = 10;

/** 缺省布局画布（与 SVG viewBox 同口径）。 */
export const LAYOUT_CANVAS = { width: 920, height: 620 } as const;

const DEFAULT_ITERATIONS = 320;
const DEFAULT_SEED = 0x9e3779b9;
const DEFAULT_LINK_DISTANCE = 96;
/** 分离收敛上限（保证「节点不重叠」不变式；超出即接受当前结果）。 */
const SEPARATION_PASSES = 60;
/** 力导向节点数上限：超过则退化为确定性螺旋概览布局（防主线程长阻塞）。 */
export const MAX_FORCE_NODES = 600;
/** 力导向总 tick 预算（迭代次数 = 预算/节点数，下限 100）——让大图不拖长首屏。 */
const TICK_BUDGET = 48000;
const MIN_ITERATIONS = 100;
/** 点阵底纹留白：布局内容与画布边缘的最小距离。 */
const CANVAS_MARGIN = 40;

export interface LayoutGraphInput {
  entities: GraphEntity[];
  edges: GraphEdge[];
}

export interface LayoutOptions {
  width?: number;
  height?: number;
  iterations?: number;
  /** 随机种子——同种子同输入必得同布局。 */
  seed?: number;
  linkDistance?: number;
}

export interface LayoutResult {
  /** 实体 id → 图坐标（键序与 entities 表一致）。 */
  positions: Map<string, { x: number; y: number }>;
  /** 内容边界（已保证落在画布内）。 */
  bounds: Bounds;
  width: number;
  height: number;
  /** `force` = 力导向收敛；`overview` = 节点超上限，走确定性网格概览布局。 */
  layoutMode: "force" | "overview";
  /** 密度自适应后的节点可视半径（视图层直接用这个值画，别再用常量 NODE_RADIUS）。 */
  nodeRadius: number;
}

interface SimNode {
  id: string;
  x: number;
  y: number;
  vx?: number;
  vy?: number;
  /** d3 的钉住坐标（非 null 时该节点不受力学驱动，用于拖拽）。 */
  fx?: number | null;
  fy?: number | null;
}

interface SimLink {
  source: string | SimNode;
  target: string | SimNode;
}

/** 功能：mulberry32 —— 小而确定的伪随机源（禁 Math.random，保布局可复现）。 */
function createSeededRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 功能：黄金角螺旋初始摆位——确定性且天然铺开，避免全部堆在原点。 */
function seedPositions(count: number, width: number, height: number): { x: number; y: number }[] {
  const centerX = width / 2;
  const centerY = height / 2;
  const maxRadius = Math.min(width, height) * 0.32;
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));

  return Array.from({ length: count }, (_, index) => {
    const radius = maxRadius * Math.sqrt((index + 0.5) / Math.max(1, count));
    const angle = index * goldenAngle;
    return { x: centerX + radius * Math.cos(angle), y: centerY + radius * Math.sin(angle) };
  });
}

/** 功能：剔除悬空与自环边后的布局用连边（脏边不参与力学）。 */
function buildLinks(edges: GraphEdge[], ids: ReadonlySet<string>): SimLink[] {
  const links: SimLink[] = [];
  const seen = new Set<string>();
  for (const edge of edges) {
    if (edge.source === edge.target) continue;
    if (!ids.has(edge.source) || !ids.has(edge.target)) continue;
    const key = `${edge.source}\u0000${edge.target}`;
    if (seen.has(key)) continue;
    seen.add(key);
    links.push({ source: edge.source, target: edge.target });
  }
  return links;
}

/** 功能：把节点位置夹回画布内——边距随画布收缩（固定 40px 边距在小画布上会把所有节点压成一条线）。 */
function clampToCanvas(nodes: SimNode[], width: number, height: number): void {
  const marginX = Math.min(CANVAS_MARGIN, width / 4);
  const marginY = Math.min(CANVAS_MARGIN, height / 4);
  const minX = Math.min(marginX, width / 2);
  const maxX = Math.max(width - marginX, width / 2);
  const minY = Math.min(marginY, height / 2);
  const maxY = Math.max(height - marginY, height / 2);

  for (const node of nodes) {
    node.x = Math.min(maxX, Math.max(minX, node.x));
    node.y = Math.min(maxY, Math.max(minY, node.y));
  }
}

/**
 * 功能：分离目标间距（= 2×密度自适应半径，见 `nodeRadiusFor`）。
 * 稀疏时就是 2×NODE_RADIUS（不重叠）；稠密时按人均可用面积退化——否则节点数超过画布容纳极限时
 * 分离会做多轮不可能收敛的徒劳功。
 */
export function separationDistance(nodeCount: number, width: number, height: number): number {
  return nodeRadiusFor(nodeCount, width, height) * 2;
}

/**
 * 功能：节点可视半径按密度自适应。
 * 固定半径下「不重叠」在密集图上物理不可达（n 个半径 r 的圆需要 ≈ n·(2r)² 面积）
 * 因此稠密时缩小圆点，让间距不变式重新可达成；下方有 MIN_NODE_RADIUS 兜底。
 */
export function nodeRadiusFor(nodeCount: number, width: number, height: number): number {
  if (nodeCount <= 1) return NODE_RADIUS;
  const areaPerNode = (width * height) / nodeCount;
  // 0.30 = 力导向实际可达到的填充效率系数（理想六方密排 ≈ 0.47，取 0.30 留出真实余量）。
  // 取太大（如 0.42）会给出物理上收敛不了的目标间距，分离会做徒劳功。
  const densityLimit = Math.sqrt(Math.max(0, areaPerNode)) * 0.3;
  return Math.max(MIN_NODE_RADIUS, Math.min(NODE_RADIUS, densityLimit));
}

/**
 * 功能：软边界力（d3-force 无内建）——节点越界时按超出量反向修正速度，让布局**自己收敛在画布内**。
 * 替代「跑完再硬夹取」：硬夹取会把越界节点全压到边界线上（400 节点时 197 个贴 y 边、2 对完全重合）
 * 之后分离算法因边界饱和而永远推不开。
 */
function forceBoundary(width: number, height: number, radius: number): Force<SimNode, undefined> {
  let nodes: SimNode[] = [];
  const marginX = Math.min(CANVAS_MARGIN, width / 4);
  const marginY = Math.min(CANVAS_MARGIN, height / 4);
  const minX = marginX + radius;
  const maxX = width - marginX - radius;
  const minY = marginY + radius;
  const maxY = height - marginY - radius;
  const stiffness = 0.9;

  const apply = (node: SimNode) => {
    if (node.x < minX) node.vx = (node.vx ?? 0) + (minX - node.x) * stiffness;
    else if (node.x > maxX) node.vx = (node.vx ?? 0) + (maxX - node.x) * stiffness;
    if (node.y < minY) node.vy = (node.vy ?? 0) + (minY - node.y) * stiffness;
    else if (node.y > maxY) node.vy = (node.vy ?? 0) + (maxY - node.y) * stiffness;
  };

  const force = () => {
    for (const node of nodes) apply(node);
  };
  force.initialize = (initial: SimNode[]) => {
    nodes = initial;
  };
  return force as Force<SimNode, undefined>;
}

/** 功能：概览布局的确定性网格初摆（比螺旋更均匀，分离只需做微调即可满足间距不变式）。 */ function gridPositions(
  count: number,
  width: number,
  height: number,
): { x: number; y: number }[] {
  const marginX = Math.min(CANVAS_MARGIN, width / 8);
  const marginY = Math.min(CANVAS_MARGIN, height / 8);
  const usableWidth = Math.max(1, width - marginX * 2);
  const usableHeight = Math.max(1, height - marginY * 2);
  const columns = Math.max(1, Math.round(Math.sqrt((count * usableWidth) / usableHeight)));
  const rows = Math.max(1, Math.ceil(count / columns));

  return Array.from({ length: count }, (_, index) => ({
    x:
      marginX +
      (columns === 1 ? usableWidth / 2 : ((index % columns) * usableWidth) / (columns - 1)),
    y:
      marginY +
      (rows === 1 ? usableHeight / 2 : (Math.floor(index / columns) * usableHeight) / (rows - 1)),
  }));
}

/**
 * 功能：节点沿 (dirX, dirY) 方向还剩多少可位移份额（0=已贴边，1=空间充足）。
 * 份额化让「被边界吸收的推力」转给对面节点——否则贴着边的节点对会互相抵住，多轮后仍重叠。
 */
function boundaryRoom(
  node: SimNode,
  dirX: number,
  dirY: number,
  width: number,
  height: number,
  reference: number,
): number {
  const marginX = Math.min(CANVAS_MARGIN, width / 4);
  const marginY = Math.min(CANVAS_MARGIN, height / 4);

  const roomX =
    dirX > 1e-9
      ? (width - marginX - node.x) / dirX
      : dirX < -1e-9
        ? (marginX - node.x) / dirX
        : Number.POSITIVE_INFINITY;
  const roomY =
    dirY > 1e-9
      ? (height - marginY - node.y) / dirY
      : dirY < -1e-9
        ? (marginY - node.y) / dirY
        : Number.POSITIVE_INFINITY;

  const room = Math.max(0, Math.min(roomX, roomY));
  return Math.min(1, room / Math.max(1e-6, reference));
}

/** 功能：任意两节点的最小中心距（用于按可达间距反推可视半径）。 */
function minPairDistance(nodes: SimNode[]): number {
  let min = Number.POSITIVE_INFINITY;
  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      min = Math.min(min, Math.hypot(nodes[i].x - nodes[j].x, nodes[i].y - nodes[j].y));
    }
  }
  return min;
}

/**
 * 功能：分离收敛——两两推开至目标间距，**推力按边界可位移份额分摊**后夹回画布。
 * ① 份额化：贴边节点份额为 0、对面拿全额推力，避免贴边节点对互相抵住（否则多轮后仍重叠）；
 * ② 网格邻域：每轮只比较周围 3×3 桶内的节点（O(n) 而非 O(n²)），让轮次可以放心加大；
 * ③ 每轮末夹回画布，作为软边界力之外的最后一道保险。
 */
function separateNodes(nodes: SimNode[], minDistance: number, width: number, height: number): void {
  if (nodes.length < 2) return;

  // 软边界力可能留少量越界，先归一化初态；此后推力受边界距离封顶，节点不再越界
  clampToCanvas(nodes, width, height);

  const cell = Math.max(1, minDistance * 2);
  const columns = Math.max(1, Math.ceil(width / cell) + 2);
  const rows = Math.max(1, Math.ceil(height / cell) + 2);

  for (let pass = 0; pass < SEPARATION_PASSES; pass += 1) {
    const buckets = new Map<number, number[]>();
    for (let index = 0; index < nodes.length; index += 1) {
      const node = nodes[index];
      const cx = Math.min(columns - 1, Math.max(0, Math.floor(node.x / cell)));
      const cy = Math.min(rows - 1, Math.max(0, Math.floor(node.y / cell)));
      const key = cy * columns + cx;
      const bucket = buckets.get(key);
      if (bucket) bucket.push(index);
      else buckets.set(key, [index]);
    }

    let moved = false;

    for (let i = 0; i < nodes.length; i += 1) {
      const left = nodes[i];
      const baseX = Math.min(columns - 1, Math.max(0, Math.floor(left.x / cell)));
      const baseY = Math.min(rows - 1, Math.max(0, Math.floor(left.y / cell)));

      for (let offsetX = -1; offsetX <= 1; offsetX += 1) {
        for (let offsetY = -1; offsetY <= 1; offsetY += 1) {
          const bucket = buckets.get((baseY + offsetY) * columns + (baseX + offsetX));
          if (!bucket) continue;

          for (const j of bucket) {
            if (j <= i) continue;
            const right = nodes[j];
            const dx = right.x - left.x;
            const dy = right.y - left.y;
            const distance = Math.hypot(dx, dy);
            if (distance >= minDistance) continue;

            moved = true;
            // 完全重合时给一个确定性方向，避免除零
            const isCoincident = distance < 1e-6;
            const unitX = isCoincident ? 1 : dx / distance;
            const unitY = isCoincident ? 0 : dy / distance;
            const deficit = minDistance - (isCoincident ? 0 : distance);

            const roomLeft = boundaryRoom(left, -unitX, -unitY, width, height, minDistance);
            const roomRight = boundaryRoom(right, unitX, unitY, width, height, minDistance);
            const totalRoom = roomLeft + roomRight;

            const shareLeft = totalRoom > 1e-6 ? roomLeft / totalRoom : 0.5;
            const shareRight = totalRoom > 1e-6 ? roomRight / totalRoom : 0.5;

            left.x -= unitX * deficit * shareLeft;
            left.y -= unitY * deficit * shareLeft;
            right.x += unitX * deficit * shareRight;
            right.y += unitY * deficit * shareRight;
          }
        }
      }
    }

    clampToCanvas(nodes, width, height);
    if (!moved) return;
  }
}

/** 功能：内容边界；零尺寸时按节点尺寸撑开（避免退化边界导致后续除零）。 */
function computeBounds(nodes: SimNode[], width: number, height: number): Bounds {
  if (nodes.length === 0) return { minX: 0, minY: 0, maxX: width, maxY: height };

  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const node of nodes) {
    minX = Math.min(minX, node.x);
    minY = Math.min(minY, node.y);
    maxX = Math.max(maxX, node.x);
    maxY = Math.max(maxY, node.y);
  }

  const minSpan = NODE_RADIUS * 2;
  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;
  if (maxX - minX < minSpan) {
    minX = Math.max(0, centerX - minSpan / 2);
    maxX = Math.min(width, centerX + minSpan / 2);
  }
  if (maxY - minY < minSpan) {
    minY = Math.max(0, centerY - minSpan / 2);
    maxY = Math.min(height, centerY + minSpan / 2);
  }
  return { minX, minY, maxX, maxY };
}

/**
 * 功能：把实体/边算成静态图坐标（力导向收敛结果）。
 * 纯函数、无副作用、无定时器——同一输入多次调用结果逐点一致。
 */
export function layoutGraph(input: LayoutGraphInput, options: LayoutOptions = {}): LayoutResult {
  const width = options.width ?? LAYOUT_CANVAS.width;
  const height = options.height ?? LAYOUT_CANVAS.height;
  const iterations = options.iterations ?? DEFAULT_ITERATIONS;
  const seed = options.seed ?? DEFAULT_SEED;
  const linkDistance = options.linkDistance ?? DEFAULT_LINK_DISTANCE;

  if (input.entities.length === 0) {
    return {
      positions: new Map(),
      bounds: { minX: 0, minY: 0, maxX: width, maxY: height },
      width,
      height,
      layoutMode: "force",
      nodeRadius: NODE_RADIUS,
    };
  }

  const ids = new Set(input.entities.map((entity) => entity.id));

  // 超大图：力导向会阻塞主线程数秒 → 跳过力学，用确定性网格概览布局
  const oversize = input.entities.length > MAX_FORCE_NODES;
  const layoutMode: LayoutResult["layoutMode"] = oversize ? "overview" : "force";

  // 半径与分离目标都由密度推出：保证「节点不重叠」在任何规模下都可达成
  const targetRadius = nodeRadiusFor(input.entities.length, width, height);

  // 力导向用黄金角螺旋初摆（力学会把它们推开）；概览模式直接用网格（力学不跑，网格即终态）
  const seeds = oversize
    ? gridPositions(input.entities.length, width, height)
    : seedPositions(input.entities.length, width, height);
  const nodes: SimNode[] = input.entities.map((entity, index) => ({
    id: entity.id,
    x: seeds[index].x,
    y: seeds[index].y,
  }));

  if (!oversize) {
    const effectiveIterations = Math.max(
      MIN_ITERATIONS,
      Math.min(iterations, Math.round(TICK_BUDGET / nodes.length)),
    );
    const links = buildLinks(input.edges, ids);

    const simulation = forceSimulation<SimNode>(nodes)
      .randomSource(createSeededRandom(seed))
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((node) => node.id)
          .distance(linkDistance)
          .strength(0.35),
      )
      .force("charge", forceManyBody<SimNode>().strength(-220).distanceMax(460).theta(1.2))
      .force("center", forceCenter<SimNode>(width / 2, height / 2))
      .force("x", forceX<SimNode>(width / 2).strength(0.035))
      .force("y", forceY<SimNode>(height / 2).strength(0.035))
      // 碰撞半径 = 目标间距的一半 → 力学阶段就逼近「不重叠」，分离阶段只做收尾
      .force("collide", forceCollide<SimNode>(targetRadius).strength(1))
      .force("boundary", forceBoundary(width, height, targetRadius))
      .stop();

    simulation.tick(effectiveIterations);
  }

  // 边界由软边界力在力学阶段处理；这里只做分离收敛（内部每轮夹取作为最后一道保险）
  separateNodes(nodes, targetRadius * 2, width, height);

  // 可视半径按**可达间距**反推：「圆点不重叠」由构造成立，与分离收敛质量无关
  // （分离无法在超密图上做到完美收敛，让半径去适配实际布点才是稳的做法）
  const achievedSpacing = minPairDistance(nodes);
  const nodeRadius = Number.isFinite(achievedSpacing)
    ? Math.max(MIN_NODE_RADIUS, Math.min(NODE_RADIUS, achievedSpacing / 2))
    : NODE_RADIUS;

  const positions = new Map<string, { x: number; y: number }>();
  for (const node of nodes) positions.set(node.id, { x: node.x, y: node.y });

  return {
    positions,
    bounds: computeBounds(nodes, width, height),
    width,
    height,
    layoutMode,
    nodeRadius,
  };
}

/* ─────────────────────────────────────────────────────────────
 * 拖拽实时重排
 * ───────────────────────────────────────────────────────────── */

/** 单次拖拽升温后的 alpha：够热以推开邻居，又不至于把整图炸散。 */
const DRAG_ALPHA = 0.35;
/** 拖拽中每帧推进的 tick 数（1 帧 1 tick ≈ 实时跟随）。 */
export const DRAG_TICKS_PER_FRAME = 1;
/** 松手后最多再推进多少 tick 让它落定。 */
export const SETTLE_TICKS = 220;

export interface GraphSimulation {
  /** 当前坐标（每次 advance 后就地更新）。 */
  positions: Map<string, { x: number; y: number }>;
  /** 钉住节点到画布坐标并升温（拖拽中反复调用）。 */
  pin(id: string, x: number, y: number): void;
  /** 解除钉住（松手），不再升温。 */
  unpin(id: string): void;
  /** 推进 ticks 帧；返回是否仍在活动（alpha 未冷却）。 */
  advance(ticks: number): boolean;
  /** 是否已冷却（可停 rAF）。 */
  settled(): boolean;
  /** 释放实例引用（无定时器，仅断开引用）。 */
  dispose(): void;
}

export interface GraphSimulationOptions {
  width: number;
  height: number;
  /** 渲染半径（拖拽期间固定沿用静态布局的半径，避免圆点忽大忽小）。 */
  nodeRadius: number;
  /** 静态布局已收敛的坐标——拖拽从当前画面无缝开始，不重跑 tick。 */
  initialPositions: Map<string, { x: number; y: number }>;
  seed?: number;
  linkDistance?: number;
}

/**
 * 功能：创建可手动驱动的实时仿真（用于拖拽节点后重排）。
 * 复用 `layoutGraph` 的同一套力学与种子；**不创建定时器**，由调用方在 rAF 里 `advance()`。
 * 概览模式（超上限）返回 null——那种规模下实时重排代价过高，调用方应退化为「松手后一次性重排」。
 */
export function createGraphSimulation(
  input: LayoutGraphInput,
  options: GraphSimulationOptions,
): GraphSimulation | null {
  const { width, height, nodeRadius, initialPositions } = options;
  if (input.entities.length === 0 || input.entities.length > MAX_FORCE_NODES) return null;

  const seed = options.seed ?? DEFAULT_SEED;
  const linkDistance = options.linkDistance ?? DEFAULT_LINK_DISTANCE;
  const seeds = seedPositions(input.entities.length, width, height);

  const nodes: SimNode[] = input.entities.map((entity, index) => {
    const existing = initialPositions.get(entity.id);
    return {
      id: entity.id,
      x: existing?.x ?? seeds[index].x,
      y: existing?.y ?? seeds[index].y,
    };
  });
  const byId = new Map(nodes.map((node) => [node.id, node]));

  const ids = new Set(input.entities.map((entity) => entity.id));
  const links = buildLinks(input.edges, ids);

  const simulation = forceSimulation<SimNode>(nodes)
    .randomSource(createSeededRandom(seed))
    .force(
      "link",
      forceLink<SimNode, SimLink>(links)
        .id((node) => node.id)
        .distance(linkDistance)
        .strength(0.35),
    )
    .force("charge", forceManyBody<SimNode>().strength(-220).distanceMax(460).theta(1.2))
    .force("center", forceCenter<SimNode>(width / 2, height / 2))
    .force("x", forceX<SimNode>(width / 2).strength(0.035))
    .force("y", forceY<SimNode>(height / 2).strength(0.035))
    .force("collide", forceCollide<SimNode>(nodeRadius).strength(1))
    .force("boundary", forceBoundary(width, height, nodeRadius))
    .stop();

  const positions = new Map<string, { x: number; y: number }>();
  const writeBack = () => {
    for (const node of nodes) {
      node.x = Math.min(width, Math.max(0, node.x));
      node.y = Math.min(height, Math.max(0, node.y));
      positions.set(node.id, { x: node.x, y: node.y });
    }
  };
  writeBack();

  return {
    positions,
    pin(id, x, y) {
      const node = byId.get(id);
      if (!node) return;
      node.fx = x;
      node.fy = y;
      node.x = x;
      node.y = y;
      simulation.alpha(DRAG_ALPHA);
      writeBack();
    },
    unpin(id) {
      const node = byId.get(id);
      if (!node) return;
      node.fx = null;
      node.fy = null;
      writeBack();
    },
    advance(ticks) {
      simulation.tick(ticks);
      writeBack();
      return !this.settled();
    },
    settled() {
      return simulation.alpha() <= simulation.alphaMin();
    },
    dispose() {
      simulation.stop();
    },
  };
}
