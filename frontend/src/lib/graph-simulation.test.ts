import { describe, expect, it } from "vitest";

import { MAX_FORCE_NODES, layoutGraph, createGraphSimulation } from "@/lib/graph-layout";
import type { GraphEdge, GraphEntity } from "@/services/graph";

function makeEntities(count: number): GraphEntity[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `e${index}`,
    type: "note" as const,
    name: `节点${index}`,
  }));
}

/** 功能：以 e0 为枢纽的星形图（e0 动 → 邻居必须跟着重排）。 */
function starGraph(leafCount: number): { edges: GraphEdge[] } {
  return {
    edges: Array.from({ length: leafCount }, (_, index) => ({
      source: "e0",
      target: `e${index + 1}`,
      type: "references",
    })),
  };
}

const CANVAS = { width: 920, height: 620 };

function distance(
  positions: Map<string, { x: number; y: number }>,
  left: string,
  right: string,
): number {
  const a = positions.get(left)!;
  const b = positions.get(right)!;
  return Math.hypot(a.x - b.x, a.y - b.y);
}

describe("createGraphSimulation", () => {
  it("概览模式规模（超上限）返回 null —— 该规模不做实时重排", () => {
    const simulation = createGraphSimulation(
      { entities: makeEntities(MAX_FORCE_NODES + 50), edges: [] },
      { ...CANVAS, nodeRadius: 6, initialPositions: new Map() },
    );
    expect(simulation).toBeNull();
  });

  it("空图返回 null", () => {
    expect(
      createGraphSimulation(
        { entities: [], edges: [] },
        { ...CANVAS, nodeRadius: 18, initialPositions: new Map() },
      ),
    ).toBeNull();
  });

  it("初始坐标沿用静态布局结果（拖拽从当前画面无缝开始）", () => {
    const entities = makeEntities(12);
    const { edges } = starGraph(11);
    const layout = layoutGraph({ entities, edges });

    const simulation = createGraphSimulation(
      { entities, edges },
      { ...CANVAS, nodeRadius: layout.nodeRadius, initialPositions: layout.positions },
    )!;

    expect(simulation).not.toBeNull();
    for (const [id, point] of layout.positions) {
      expect(simulation.positions.get(id)).toEqual(point);
    }
  });

  it("pin 后推进：被钉节点纹丝不动，邻居被推开（实时重排真的发生）", () => {
    const entities = makeEntities(12);
    const { edges } = starGraph(11);
    const layout = layoutGraph({ entities, edges });
    const simulation = createGraphSimulation(
      { entities, edges },
      { ...CANVAS, nodeRadius: layout.nodeRadius, initialPositions: layout.positions },
    )!;

    const beforeNeighborDistance = distance(simulation.positions, "e0", "e1");

    // 把枢纽 e0 拖到画布左上角
    simulation.pin("e0", 120, 120);
    for (let frame = 0; frame < 40; frame += 1) simulation.advance(1);

    // 钉住处严格不动
    expect(simulation.positions.get("e0")).toEqual({ x: 120, y: 120 });
    // 邻居重排：与 e0 的距离发生变化
    expect(distance(simulation.positions, "e0", "e1")).not.toBeCloseTo(beforeNeighborDistance, 0);
  });

  it("unpin 后被钉节点恢复受力（坐标开始变化）", () => {
    const entities = makeEntities(10);
    const { edges } = starGraph(9);
    const layout = layoutGraph({ entities, edges });
    const simulation = createGraphSimulation(
      { entities, edges },
      { ...CANVAS, nodeRadius: layout.nodeRadius, initialPositions: layout.positions },
    )!;

    simulation.pin("e0", 150, 150);
    simulation.advance(30);
    const pinned = { ...simulation.positions.get("e0")! };

    simulation.unpin("e0");
    for (let frame = 0; frame < 30; frame += 1) simulation.advance(1);

    const released = simulation.positions.get("e0")!;
    expect(Math.hypot(released.x - pinned.x, released.y - pinned.y)).toBeGreaterThan(0.5);
  });

  it("advance 返回活动态，持续推进后 settled 转为 true（调用方可停 rAF）", () => {
    const entities = makeEntities(8);
    const layout = layoutGraph({ entities, edges: [] });
    const simulation = createGraphSimulation(
      { entities, edges: [] },
      { ...CANVAS, nodeRadius: layout.nodeRadius, initialPositions: layout.positions },
    )!;

    expect(simulation.settled()).toBe(false);
    const stillRunning = simulation.advance(30);
    expect(stillRunning).toBe(true);

    simulation.advance(600);
    expect(simulation.settled()).toBe(true);
    expect(simulation.advance(1)).toBe(false);
  });

  it("坐标恒在画布内（拖到画布外也会被夹回）", () => {
    const entities = makeEntities(6);
    const layout = layoutGraph({ entities, edges: [] });
    const simulation = createGraphSimulation(
      { entities, edges: [] },
      { ...CANVAS, nodeRadius: layout.nodeRadius, initialPositions: layout.positions },
    )!;

    simulation.pin("e0", -500, 9999);
    simulation.advance(5);

    const pinned = simulation.positions.get("e0")!;
    expect(pinned.x).toBeGreaterThanOrEqual(0);
    expect(pinned.x).toBeLessThanOrEqual(CANVAS.width);
    expect(pinned.y).toBeGreaterThanOrEqual(0);
    expect(pinned.y).toBeLessThanOrEqual(CANVAS.height);
  });

  it("确定性：同输入同种子 → 同序列推进得到相同坐标", () => {
    const entities = makeEntities(10);
    const { edges } = starGraph(9);
    const layout = layoutGraph({ entities, edges });
    const options = {
      ...CANVAS,
      nodeRadius: layout.nodeRadius,
      initialPositions: layout.positions,
    };

    const first = createGraphSimulation({ entities, edges }, options)!;
    const second = createGraphSimulation({ entities, edges }, options)!;

    first.pin("e0", 200, 200);
    second.pin("e0", 200, 200);
    first.advance(25);
    second.advance(25);

    for (const [id, point] of first.positions) {
      expect(second.positions.get(id)).toEqual(point);
    }
  });

  it("未知 id 的 pin/unpin 不崩（脏数据防御）", () => {
    const entities = makeEntities(4);
    const layout = layoutGraph({ entities, edges: [] });
    const simulation = createGraphSimulation(
      { entities, edges: [] },
      { ...CANVAS, nodeRadius: layout.nodeRadius, initialPositions: layout.positions },
    )!;

    expect(() => simulation.pin("ghost", 10, 10)).not.toThrow();
    expect(() => simulation.unpin("ghost")).not.toThrow();
    expect(simulation.positions.size).toBe(4);
  });
});
