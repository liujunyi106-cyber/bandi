import { describe, expect, it } from "vitest";

import { filterSnapshot, type GraphSnapshot } from "@/services/graph";

/** 测试图：p1—t1—t2 链 + 孤立点 n1 + 中文实体 p2（引用链）。 */
const FIXTURE: GraphSnapshot = {
  entities: [
    { id: "p1", type: "project", name: "Bandi Agent" },
    { id: "t1", type: "task", name: "实现图谱" },
    { id: "t2", type: "task", name: "力导向布局" },
    { id: "n1", type: "note", name: "孤立笔记" },
    { id: "p2", type: "project", name: "知识组织专项" },
    { id: "k1", type: "knowledge", name: "布局算法笔记" },
  ],
  edges: [
    { source: "p1", target: "t1", type: "contains" },
    { source: "t1", target: "t2", type: "contains" },
    { source: "p2", target: "k1", type: "references" },
  ],
};

describe("filterSnapshot", () => {
  it("空/空白查询 → 原样返回，隐藏计数为 0", () => {
    for (const query of ["", "   "]) {
      const result = filterSnapshot(FIXTURE, query);
      expect(result.snapshot.entities).toHaveLength(6);
      expect(result.snapshot.edges).toHaveLength(3);
      expect(result.hiddenEntityCount).toBe(0);
      expect(result.hiddenEdgeCount).toBe(0);
    }
  });

  it("命中实体保留其一跳邻域（上下文可读），匹配不区分大小写", () => {
    const result = filterSnapshot(FIXTURE, "bandi");
    const ids = result.snapshot.entities.map((entity) => entity.id).sort();
    // 命中 p1；邻域带进 t1；t2 是二跳，不保留；n1/p2/k1 无关
    expect(ids).toEqual(["p1", "t1"]);
    // 只保留两端都在的边
    expect(result.snapshot.edges).toEqual([{ source: "p1", target: "t1", type: "contains" }]);
    expect(result.hiddenEntityCount).toBe(4);
    expect(result.hiddenEdgeCount).toBe(2);
  });

  it("中文实体名可命中", () => {
    const result = filterSnapshot(FIXTURE, "知识组织");
    expect(result.snapshot.entities.map((entity) => entity.id)).toEqual(["p2", "k1"]);
  });

  it("多命中时取并集，公共邻域不重复；两命中间的边保留", () => {
    // 「布局」同时命中 t2 与 k1（力导向布局 / 布局算法笔记）
    const result = filterSnapshot(FIXTURE, "布局");
    const ids = result.snapshot.entities.map((entity) => entity.id).sort();
    // t2 带进邻域 t1；k1 带进邻域 p2
    expect(ids).toEqual(["k1", "p2", "t1", "t2"]);
  });

  it("无命中 → 空图（不是报错），页面据此回落空态", () => {
    const result = filterSnapshot(FIXTURE, "不存在的词");
    expect(result.snapshot.entities).toEqual([]);
    expect(result.snapshot.edges).toEqual([]);
    expect(result.hiddenEntityCount).toBe(6);
  });

  it("相邻关系边类型不影响邻域判定（任何类型的边都算邻接）", () => {
    const result = filterSnapshot(FIXTURE, "实现图谱");
    // t1 的邻域 = p1 + t2（contains 边双向邻接）
    expect(result.snapshot.entities.map((entity) => entity.id).sort()).toEqual(["p1", "t1", "t2"]);
  });
});
