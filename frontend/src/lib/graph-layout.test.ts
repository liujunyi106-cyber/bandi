import { describe, expect, it } from "vitest";

import {
  LAYOUT_CANVAS,
  MAX_FORCE_NODES,
  MIN_NODE_RADIUS,
  NODE_RADIUS,
  layoutGraph,
  nodeRadiusFor,
  separationDistance,
  type LayoutGraphInput,
} from "@/lib/graph-layout";
import type { GraphEdge, GraphEntity } from "@/services/graph";

/** 功能：造 n 个 note 实体（确定性 id，避免随机测试数据）。 */
function makeEntities(count: number): GraphEntity[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `e${index}`,
    type: "note" as const,
    name: `节点${index}`,
  }));
}

/** 功能：环形连边（保证图连通且度数均匀）。 */
function makeRingEdges(count: number): GraphEdge[] {
  return Array.from({ length: count }, (_, index) => ({
    source: `e${index}`,
    target: `e${(index + 1) % count}`,
    type: "references",
  }));
}

/** 功能：任意两节点的最小中心距。 */
function minPairDistance(positions: Map<string, { x: number; y: number }>): number {
  const points = [...positions.values()];
  let min = Number.POSITIVE_INFINITY;
  for (let i = 0; i < points.length; i += 1) {
    for (let j = i + 1; j < points.length; j += 1) {
      const dx = points[i].x - points[j].x;
      const dy = points[i].y - points[j].y;
      min = Math.min(min, Math.hypot(dx, dy));
    }
  }
  return min;
}

describe("layoutGraph", () => {
  it("确定性：同输入 + 同种子 → 坐标逐点完全一致", () => {
    const input: LayoutGraphInput = { entities: makeEntities(24), edges: makeRingEdges(24) };
    const first = layoutGraph(input);
    const second = layoutGraph(input);

    expect([...first.positions.keys()]).toEqual([...second.positions.keys()]);
    for (const [id, point] of first.positions) {
      expect(second.positions.get(id)).toEqual(point);
    }
  });

  it("每个实体都拿到有限坐标，且落在画布边界内", () => {
    const input: LayoutGraphInput = { entities: makeEntities(40), edges: makeRingEdges(40) };
    const { positions, bounds } = layoutGraph(input);

    expect(positions.size).toBe(40);
    for (const point of positions.values()) {
      expect(Number.isFinite(point.x)).toBe(true);
      expect(Number.isFinite(point.y)).toBe(true);
      expect(point.x).toBeGreaterThanOrEqual(bounds.minX);
      expect(point.x).toBeLessThanOrEqual(bounds.maxX);
      expect(point.y).toBeGreaterThanOrEqual(bounds.minY);
      expect(point.y).toBeLessThanOrEqual(bounds.maxY);
    }
  });

  it("碰撞力生效：收敛后任意两节点中心距不小于节点半径（不叠在一起）", () => {
    const input: LayoutGraphInput = { entities: makeEntities(36), edges: makeRingEdges(36) };
    const { positions } = layoutGraph(input);
    expect(minPairDistance(positions)).toBeGreaterThanOrEqual(NODE_RADIUS);
  });

  it("孤立节点（无边）也有坐标，且不与其它节点重叠", () => {
    const input: LayoutGraphInput = {
      entities: makeEntities(12),
      edges: [{ source: "e0", target: "e1", type: "references" }],
    };
    const { positions } = layoutGraph(input);

    expect(positions.size).toBe(12);
    expect(positions.get("e11")).toBeDefined();
    expect(minPairDistance(positions)).toBeGreaterThanOrEqual(NODE_RADIUS);
  });

  it("悬空边（指向不存在的实体）被忽略：不崩、不新增坐标、不污染布局", () => {
    const entities = makeEntities(6);
    const withDangling = layoutGraph({
      entities,
      edges: [{ source: "e0", target: "ghost", type: "references" }],
    });
    const withoutDangling = layoutGraph({ entities, edges: [] });

    expect(withDangling.positions.size).toBe(6);
    expect([...withDangling.positions.keys()]).toEqual([...withoutDangling.positions.keys()]);
  });

  it("自环边不崩且不参与布局力", () => {
    const input: LayoutGraphInput = {
      entities: makeEntities(5),
      edges: [{ source: "e0", target: "e0", type: "references" }],
    };
    const { positions } = layoutGraph(input);
    expect(positions.size).toBe(5);
    for (const point of positions.values()) expect(Number.isFinite(point.x)).toBe(true);
  });

  it("空图：空坐标集 + 合法 bounds（不产生 NaN）", () => {
    const { positions, bounds } = layoutGraph({ entities: [], edges: [] });
    expect(positions.size).toBe(0);
    expect(Number.isFinite(bounds.minX)).toBe(true);
    expect(Number.isFinite(bounds.maxY)).toBe(true);
  });

  it("单节点：居中且不退化为非法变换", () => {
    const { positions, bounds } = layoutGraph({
      entities: [{ id: "only", type: "project", name: "唯一" }],
      edges: [],
    });
    expect(positions.size).toBe(1);
    expect(bounds.maxX - bounds.minX).toBeGreaterThan(0);
    expect(bounds.maxY - bounds.minY).toBeGreaterThan(0);
  });

  it("规模增长近线性：200 节点耗时不超过 50 节点的 10 倍（防 O(n²) 退化）", () => {
    // 原实现用**绝对阈值**（400 节点 < 4000ms）。该用例单次约 800ms，但在并行跑
    // pytest 等负载下可达 5500ms ⇒ **负载敏感假红**（，隔离复跑转绿）。
    // 改为**相对基线**：4× 节点在近线性下约 4× 耗时
    // 而 O(n²) 会到 16× ⇒ 10× 阈值可区分退化，且同进程内测量让机器负载对分子分母
    // 影响相抵。另留一个**宽松绝对上限**兜底（真死循环 / 指数退化仍会红）。
    // 规模取 50/200（同为 4× ⇒ O(n²) 仍 16×，判别力不变）而非 100/400：复测
    // 发现重负载下 100+400 的 bestOf 总耗时 > vitest 默认 用例超时 ⇒ 假红复发；
    // 总工作量降为 1/4，并显式放宽用例超时到 。
    const build = (count: number): LayoutGraphInput => ({
      entities: makeEntities(count),
      edges: Array.from({ length: count * 2 }, (_, index) => ({
        source: `e${index % count}`,
        target: `e${(index * 7 + 13) % count}`,
        type: "references",
      })),
    });
    /** 功能：预热一次后取多轮最小值（削掉 首次调用的优化开销与偶发抖动）。 */
    const bestOf = (count: number, rounds = 3): number => {
      layoutGraph(build(count)); // 预热
      let best = Number.POSITIVE_INFINITY;
      for (let round = 0; round < rounds; round += 1) {
        const startedAt = Date.now();
        layoutGraph(build(count));
        best = Math.min(best, Date.now() - startedAt);
      }
      return best;
    };

    const small = bestOf(50);
    const large = bestOf(200);

    expect(layoutGraph(build(200)).positions.size).toBe(200);
    // 相对判据 + 常数缓冲（固定开销在两端占比不同）
    expect(large).toBeLessThan(small * 10 + 300);
    expect(large).toBeLessThan(10_000);
  }, 60_000);

  it("画布尺寸可由参数覆盖（缺省用 LAYOUT_CANVAS）", () => {
    const { bounds } = layoutGraph(
      { entities: makeEntities(8), edges: [] },
      { width: 640, height: 480 },
    );
    expect(bounds.maxX - bounds.minX).toBeLessThanOrEqual(640);
    expect(bounds.maxY - bounds.minY).toBeLessThanOrEqual(480);
    expect(LAYOUT_CANVAS.width).toBeGreaterThan(0);
  });

  it("上限内标注力导向模式；每个实体都有坐标", () => {
    const { layoutMode, positions } = layoutGraph({ entities: makeEntities(10), edges: [] });
    expect(layoutMode).toBe("force");
    expect(positions.size).toBe(10);
  });

  it("超过力导向上限时退化为概览布局并如实标注模式（不阻塞主线程）", () => {
    const entities = makeEntities(MAX_FORCE_NODES + 150);
    const startedAt = Date.now();
    const { positions, layoutMode } = layoutGraph({ entities, edges: [] });
    const elapsed = Date.now() - startedAt;

    expect(layoutMode).toBe("overview");
    expect(positions.size).toBe(entities.length);
    expect(elapsed).toBeLessThan(3000);
  });

  it("概览模式（超密）下仍保证节点互不重叠（按密度自适应后的半径衡量）", () => {
    const { positions, nodeRadius } = layoutGraph({
      entities: makeEntities(MAX_FORCE_NODES + 150),
      edges: [],
    });
    expect(minPairDistance(positions)).toBeGreaterThanOrEqual(nodeRadius * 2);
    // 独立于自适应半径的绝对下界（与半径派生的断言解耦——抓「硬夹取致完全重合」类回归）
    expect(minPairDistance(positions)).toBeGreaterThanOrEqual(MIN_NODE_RADIUS * 2);
  });

  it("力导向模式同样按自适应半径保证不重叠", () => {
    const { positions, nodeRadius } = layoutGraph({
      entities: makeEntities(400),
      edges: [],
    });
    expect(minPairDistance(positions)).toBeGreaterThanOrEqual(nodeRadius * 2);
    expect(minPairDistance(positions)).toBeGreaterThanOrEqual(MIN_NODE_RADIUS * 2);
  }, 20000);
});

describe("nodeRadiusFor（密度自适应半径）", () => {
  it("稀疏时用满 NODE_RADIUS", () => {
    expect(nodeRadiusFor(5, 920, 620)).toBe(NODE_RADIUS);
    expect(nodeRadiusFor(1, 920, 620)).toBe(NODE_RADIUS);
  });

  it("稠密时缩小，且不低于 MIN_NODE_RADIUS（极端稠密触底不归零）", () => {
    const dense = nodeRadiusFor(800, 920, 620);
    expect(dense).toBeLessThan(NODE_RADIUS);
    expect(dense).toBeGreaterThanOrEqual(MIN_NODE_RADIUS);

    // 远超画布容纳量的规模只会缩到下限，不会缩成 0 或负数
    expect(nodeRadiusFor(1_000_000, 920, 620)).toBe(MIN_NODE_RADIUS);
  });

  it("分离目标恒为两倍半径（稀疏 36 / 稠密更小）", () => {
    expect(separationDistance(5, 920, 620)).toBe(NODE_RADIUS * 2);
    expect(separationDistance(800, 920, 620)).toBe(nodeRadiusFor(800, 920, 620) * 2);
  });
});
