import { describe, expect, it } from "vitest";

import { describeGraph, normalizeSnapshot, selectRenderableSubgraph } from "@/services/graph";

describe("normalizeSnapshot", () => {
  it("缺字段 / 非对象输入一律退化为空快照（不崩）", () => {
    for (const raw of [undefined, null, 42, "boom", {}, { entities: null }]) {
      const snapshot = normalizeSnapshot(raw);
      expect(snapshot.entities).toEqual([]);
      expect(snapshot.edges).toEqual([]);
    }
  });

  it("丢弃缺 id 的实体，name 缺失时回落到 id", () => {
    const snapshot = normalizeSnapshot({
      entities: [
        { id: "a", type: "note", name: "笔记 A" },
        { id: "", type: "note", name: "空 id" },
        { type: "note", name: "无 id" },
        { id: "b", type: "note" },
      ],
      edges: [],
    });

    expect(snapshot.entities.map((e) => e.id)).toEqual(["a", "b"]);
    expect(snapshot.entities[1].name).toBe("b");
  });

  it("动态类名原样保留（用户声明无白名单），缺 type 归 unknown", () => {
    const snapshot = normalizeSnapshot({
      entities: [
        { id: "x", type: "workflow", name: "自定义类" },
        { id: "y", name: "无类型" },
      ],
      edges: [],
    });
    expect(snapshot.entities).toHaveLength(2);
    expect(snapshot.entities[0].type).toBe("workflow");
    expect(snapshot.entities[1].type).toBe("unknown");
  });

  it("重复 id 只保留首个出现", () => {
    const snapshot = normalizeSnapshot({
      entities: [
        { id: "dup", type: "task", name: "先出现的" },
        { id: "dup", type: "task", name: "后出现的" },
      ],
      edges: [],
    });
    expect(snapshot.entities).toHaveLength(1);
    expect(snapshot.entities[0].name).toBe("先出现的");
  });

  it("边归一：丢弃悬空边与自环、去重、字段缺失容错", () => {
    const snapshot = normalizeSnapshot({
      entities: [
        { id: "a", type: "note", name: "A" },
        { id: "b", type: "note", name: "B" },
      ],
      edges: [
        { source: "a", target: "b", type: "references" },
        { source: "a", target: "b", type: "references" },
        { source: "a", target: "ghost", type: "references" },
        { source: "a", target: "a", type: "references" },
        { source: "b", target: "a" },
        { source: null, target: "b", type: "references" },
      ],
    });

    expect(snapshot.edges).toEqual([
      { source: "a", target: "b", type: "references" },
      { source: "b", target: "a", type: "" },
    ]);
  });
});

describe("describeGraph", () => {
  const snapshot = normalizeSnapshot({
    entities: [
      { id: "p1", type: "project", name: "项目一" },
      { id: "t1", type: "task", name: "任务一" },
      { id: "t2", type: "task", name: "任务二" },
      { id: "n1", type: "note", name: "孤立笔记" },
      { id: "k1", type: "knowledge", name: "知识一" },
      { id: "c1", type: "contact", name: "联系人" },
    ],
    edges: [
      { source: "t1", target: "p1", type: "belongs_to" },
      { source: "t1", target: "c1", type: "assigned_to" },
      { source: "k1", target: "p1", type: "references" },
    ],
  });

  it("统计实体数 / 边数 / 五类分布", () => {
    const summary = describeGraph(snapshot);
    expect(summary.entityCount).toBe(6);
    expect(summary.edgeCount).toBe(3);
    expect(summary.typeCounts).toEqual({
      project: 1,
      task: 2,
      note: 1,
      contact: 1,
      knowledge: 1,
    });
  });

  it("识别孤立实体（入度出度均为 0），顺序跟随实体表", () => {
    const summary = describeGraph(snapshot);
    // t2（无任何边）与 n1（无任何边）都是孤立点，保持实体表顺序
    expect(summary.orphanEntityIds).toEqual(["t2", "n1"]);
  });

  it("识别度数最高的枢纽实体（用于默认聚焦与空态提示）", () => {
    const summary = describeGraph(snapshot);
    const maxDegree = Math.max(...summary.hubs.map((hub) => hub.degree));
    expect(maxDegree).toBe(2);
    expect(summary.hubs[0].degree).toBe(maxDegree);
    expect(summary.hubs.map((hub) => hub.id)).toEqual(expect.arrayContaining(["t1", "p1"]));
    expect(summary.hubs.length).toBeLessThanOrEqual(5);
  });

  it("空图：全零统计且无枢纽，不崩", () => {
    const summary = describeGraph({ entities: [], edges: [] });
    expect(summary.entityCount).toBe(0);
    expect(summary.edgeCount).toBe(0);
    expect(summary.orphanEntityIds).toEqual([]);
    expect(summary.hubs).toEqual([]);
  });
});

describe("selectRenderableSubgraph", () => {
  /** 功能：造一条链 e0-e1-...-e{n-1}，度数两端低中间高。 */
  function chain(count: number) {
    return normalizeSnapshot({
      entities: Array.from({ length: count }, (_, i) => ({
        id: `e${i}`,
        type: "note",
        name: `n${i}`,
      })),
      edges: Array.from({ length: count - 1 }, (_, i) => ({
        source: `e${i}`,
        target: `e${i + 1}`,
        type: "references",
      })),
    });
  }

  it("未超限时原样返回且不标注折叠", () => {
    const snapshot = chain(10);
    const result = selectRenderableSubgraph(snapshot, 20);
    expect(result.truncated).toBe(false);
    expect(result.snapshot).toBe(snapshot);
    expect(result.hiddenEntityCount).toBe(0);
  });

  it("超限时按度数保留高连接实体，并如实上报隐藏数量", () => {
    const snapshot = chain(60);
    const result = selectRenderableSubgraph(snapshot, 10);

    expect(result.truncated).toBe(true);
    expect(result.snapshot.entities).toHaveLength(10);
    expect(result.hiddenEntityCount).toBe(50);
    // 链两端度数最低（1），应被优先折叠；中间节点度数 2 保留
    expect(result.snapshot.entities.map((e) => e.id)).not.toContain("e0");
    expect(result.snapshot.entities.map((e) => e.id)).not.toContain("e59");
  });

  it("只保留两端都在子图内的边（不留悬空边）", () => {
    const snapshot = chain(60);
    const result = selectRenderableSubgraph(snapshot, 10);
    const kept = new Set(result.snapshot.entities.map((e) => e.id));

    expect(result.snapshot.edges.length).toBeGreaterThan(0);
    for (const edge of result.snapshot.edges) {
      expect(kept.has(edge.source)).toBe(true);
      expect(kept.has(edge.target)).toBe(true);
    }
    expect(result.hiddenEdgeCount).toBe(snapshot.edges.length - result.snapshot.edges.length);
  });

  it("确定性：同输入两次选择结果完全一致", () => {
    const snapshot = chain(60);
    const first = selectRenderableSubgraph(snapshot, 12);
    const second = selectRenderableSubgraph(snapshot, 12);
    expect(first.snapshot.entities.map((e) => e.id)).toEqual(
      second.snapshot.entities.map((e) => e.id),
    );
  });
});
