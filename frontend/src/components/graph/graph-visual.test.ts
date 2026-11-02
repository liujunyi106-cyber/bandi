/**
 * 功能：graph-visual 视觉契约测试（标签按字素截断——不拆 emoji 代理对/ZWJ 组合；
 * 配色：图谱灰阶 5 档按首次出现顺序分配——图谱内不用彩色；
 * 半径：按连接度分 5 档梯度，最大档=基准半径不引入重叠）。
 */
import { describe, expect, it } from "vitest";

import {
  buildNodeRadiusMap,
  buildTypeColorMap,
  truncateLabel,
  typeColorOf,
} from "@/components/graph/graph-visual";
import type { GraphEdge, GraphEntity } from "@/services/graph";
import { UNKNOWN_ENTITY_TYPE } from "@/services/graph";

describe("truncateLabel（按字素截断）", () => {
  it("短名不截断（中文/普通文本），首尾空白去除", () => {
    expect(truncateLabel("甲项目")).toBe("甲项目");
    expect(truncateLabel(" 乙任务 ")).toBe("乙任务");
    expect(truncateLabel("a".repeat(12))).toBe("a".repeat(12));
  });

  it("超长按字素截断并加省略号", () => {
    expect(truncateLabel("a".repeat(13))).toBe(`${"a".repeat(12)}…`);
    expect(truncateLabel("一二三四五六七八九十十一十二十三")).toBe("一二三四五六七八九十十一…");
  });

  it("破坏者：代理对 emoji 不被切半（旧实现 slice 会产生半个代理对）", () => {
    const value = "🚀".repeat(13);
    const result = truncateLabel(value, 12);
    expect(result).toBe(`${"🚀".repeat(12)}…`);
    expect(result).not.toContain("\uFFFD");
  });

  it("破坏者：ZWJ 组合 emoji（家庭）视为一个字素不拆", () => {
    const family = "👨‍👩‍👧";
    const value = family.repeat(13);
    expect(truncateLabel(value, 12)).toBe(`${family.repeat(12)}…`);
  });
});

describe("buildTypeColorMap（灰阶 5 档按首次出现顺序）", () => {
  it("顺序取灰阶槽位且 5 类内零重复（同类同灰）", () => {
    const map = buildTypeColorMap(["客户", "人员", "订单"]);
    expect(map.get("客户")).toBe("var(--graph-gray-1)");
    expect(map.get("人员")).toBe("var(--graph-gray-2)");
    expect(map.get("订单")).toBe("var(--graph-gray-3)");
    expect(new Set(map.values()).size).toBe(3);
  });

  it("重复类型不重复占位；超 5 类循环回卷", () => {
    const map = buildTypeColorMap(["a", "a", "b", "c", "d", "e", "f"]);
    expect(map.get("a")).toBe("var(--graph-gray-1)");
    expect(map.get("f")).toBe("var(--graph-gray-1)");
  });

  it("破坏者：未知类型不进映射、取色回落灰（不得被误读为真实类）", () => {
    const map = buildTypeColorMap(["客户", UNKNOWN_ENTITY_TYPE, ""]);
    expect(map.size).toBe(1);
    expect(typeColorOf(map, UNKNOWN_ENTITY_TYPE)).toBe("var(--muted-foreground)");
    expect(typeColorOf(map, "不存在类")).toBe("var(--muted-foreground)");
  });
});

describe("buildNodeRadiusMap（连接度 → 5 档半径梯度）", () => {
  const entity = (id: string): GraphEntity => ({ id, type: "note", name: id });

  it("度数越高半径越大；最大档 = 基准半径（间距不变式不破）", () => {
    const entities = [entity("hub"), entity("mid"), entity("leaf")];
    const edges: GraphEdge[] = [
      { source: "hub", target: "mid", type: "r" },
      { source: "hub", target: "leaf", type: "r" },
    ];
    const map = buildNodeRadiusMap(entities, edges, 12);
    expect(map.get("hub")).toBe(12);
    expect(map.get("mid")!).toBeLessThan(map.get("hub")!);
    expect(map.get("leaf")).toBe(map.get("mid"));
    expect(map.get("leaf")!).toBeGreaterThanOrEqual(3);
  });

  it("全部同度退化为统一基准半径；空图返回空映射", () => {
    const entities = [entity("a"), entity("b"), entity("c")];
    const map = buildNodeRadiusMap(entities, [], 10);
    expect([...map.values()]).toEqual([10, 10, 10]);
    expect(buildNodeRadiusMap([], [], 10).size).toBe(0);
  });

  it("确定性：同输入两次调用逐点一致（同度数必同档）", () => {
    const entities = [entity("a"), entity("b"), entity("c"), entity("d"), entity("e")];
    const edges: GraphEdge[] = [
      { source: "a", target: "b", type: "r" },
      { source: "a", target: "c", type: "r" },
      { source: "a", target: "d", type: "r" },
      { source: "b", target: "c", type: "r" },
      { source: "e", target: "d", type: "r" },
    ];
    const first = buildNodeRadiusMap(entities, edges, 13);
    const second = buildNodeRadiusMap(entities, edges, 13);
    for (const entity of entities) {
      expect(second.get(entity.id)).toBe(first.get(entity.id));
    }
    // a 度数 3 最大；同度的 b/c/d 取同档
    expect(first.get("a")).toBe(13);
    expect(first.get("b")).toBe(first.get("c"));
    expect(first.get("b")).toBe(first.get("d"));
  });

  it("破坏者：悬空边/自环不参与度数统计（不会把节点抬进高档）", () => {
    const entities = [entity("a"), entity("b")];
    const edges: GraphEdge[] = [
      { source: "a", target: "b", type: "r" },
      { source: "a", target: "a", type: "r" },
      { source: "a", target: "missing", type: "r" },
    ];
    const map = buildNodeRadiusMap(entities, edges, 10);
    // 有效边只有 a-b：自环与悬空边若被计入会把 a 抬成更大档
    expect(map.get("a")).toBe(10);
    expect(map.get("b")).toBe(10);
  });
});
