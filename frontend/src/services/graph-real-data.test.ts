import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { NODE_RADIUS, layoutGraph } from "@/lib/graph-layout";
import { describeGraph, normalizeSnapshot } from "@/services/graph";

/**
 * 功能：真实后端产物回归（@base docs/测试与评测标准.md 真实数据形态要求）。
 * fixture 由**生产 EntityStore + EntityGraph.export_snapshot()** 跑出（样本库 5 实体 / 3 边）
 * 不是手写的理想 JSON —— 任何字段改名/契约漂移都会在这里断掉。
 */
const FIXTURE_PATH = path.resolve(
  process.cwd(),
  "src/services/__fixtures__/graph-snapshot.sample.json",
);

const raw = JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as unknown;

describe("真实后端图谱产物", () => {
  it("归一化零丢弃（字段命名与前端契约一致）", () => {
    const snapshot = normalizeSnapshot(raw);
    const original = raw as { entities: unknown[]; edges: unknown[] };

    expect(snapshot.entities).toHaveLength(original.entities.length);
    expect(snapshot.edges).toHaveLength(original.edges.length);
  });

  it("统计出与后端一致的结构（4 个非孤立 + 1 个孤立，p-001 为枢纽）", () => {
    const summary = describeGraph(normalizeSnapshot(raw));

    expect(summary.entityCount).toBe(5);
    expect(summary.edgeCount).toBe(3);
    expect(summary.orphanEntityIds).toEqual(["n-001"]);
    expect(summary.typeCounts).toEqual({
      project: 1,
      task: 1,
      note: 1,
      contact: 1,
      knowledge: 1,
    });
    expect(summary.hubs[0]).toEqual({ id: "p-001", degree: 2 });
  });

  it("真实形态可直接布局：坐标齐备、无重叠、可复现", () => {
    const snapshot = normalizeSnapshot(raw);
    const first = layoutGraph(snapshot);
    const second = layoutGraph(snapshot);

    expect(first.positions.size).toBe(5);
    for (const [id, point] of first.positions) {
      expect(second.positions.get(id)).toEqual(point);
      expect(Number.isFinite(point.x)).toBe(true);
      expect(Number.isFinite(point.y)).toBe(true);
    }

    const points = [...first.positions.values()];
    for (let i = 0; i < points.length; i += 1) {
      for (let j = i + 1; j < points.length; j += 1) {
        expect(
          Math.hypot(points[i].x - points[j].x, points[i].y - points[j].y),
        ).toBeGreaterThanOrEqual(NODE_RADIUS);
      }
    }
  });

  it("中文实体名原样保留（不做编码/截断处理）", () => {
    const snapshot = normalizeSnapshot(raw);
    const names = snapshot.entities.map((entity) => entity.name);
    expect(names).toContain("实现知识图谱可视化");
    expect(names).toContain("张三");
  });
});
