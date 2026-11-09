import { describe, expect, it } from "vitest";

import {
  buildFlowEdges,
  buildFlowNodes,
  isReworkEdge,
  layoutSop,
  parseSop,
  readPosition,
  renameNode,
  serializeSop,
  setNodePosition,
  validateSop,
  type SopCard,
} from "@/lib/sop-graph";

const card = {
  skill_id: "new-product-launch",
  name: "新品上架 SOP",
  version: "1.2.0",
  business_domain: "电商",
  description: "从资料收集到上架",
  enabled: true,
  goal: ["完成上架"],
  trigger_intents: ["上架"],
  user_utterance_examples: [],
  required_info: [],
  slot_filling_policy: {},
  response_rules: [],
  interruption_policy: {},
  start_node_id: "start",
  terminal_node_ids: ["end"],
  nodes: [
    { node_id: "start", name: "开始", type: "start", instruction: "接收请求" },
    { node_id: "collect", name: "收集信息", type: "collect_info", instruction: "收集商品信息" },
    { node_id: "end", name: "结束", type: "terminal", instruction: "完成上架" },
  ],
  edges: [
    { source_node_id: "start", next_node_id: "collect", condition: null, priority: 0, label: null },
    { source_node_id: "collect", next_node_id: "end", condition: null, priority: 0, label: "完成" },
  ],
} as unknown as SopCard;

function clone(value: SopCard): SopCard {
  return JSON.parse(JSON.stringify(value)) as SopCard;
}

describe("sop-graph 解析与序列化", () => {
  it("解析合法 JSON 并补齐缺省字段", () => {
    const parsed = parseSop(
      JSON.stringify({
        skill_id: "simple",
        name: "简单 SOP",
        start_node_id: "start",
        terminal_node_ids: ["end"],
        nodes: [{ node_id: "start", name: "开始" }],
        edges: [],
      }),
    );
    expect(parsed.skill_id).toBe("simple");
    expect(parsed.version).toBe("1.0.0");
    expect(parsed.nodes[0].instruction).toBe("");
    expect(parsed.nodes[0].expected_user_info).toEqual([]);
    expect(parsed.nodes[0].metadata).toEqual({});
    expect(parsed.edges).toEqual([]);
  });

  it("非法 JSON 明确报错", () => {
    expect(() => parseSop("{")).toThrow("SOP JSON 不合法");
  });

  it("序列化保持字段并支持往返", () => {
    const normalized = parseSop(serializeSop(card));
    const roundTrip = parseSop(serializeSop(normalized));
    expect(roundTrip).toEqual(normalized);
    expect(serializeSop(normalized).endsWith("\n")).toBe(false);
  });
});

describe("sop-graph 校验", () => {
  it("合法卡片无错误", () => {
    expect(validateSop(card)).toEqual([]);
  });

  it("重复 node_id、缺失 start/terminal、边引用缺失节点均报错", () => {
    const broken = clone(card);
    broken.nodes.push({ ...broken.nodes[0] });
    broken.start_node_id = "missing";
    broken.terminal_node_ids = ["missing-end"];
    broken.edges.push({
      source_node_id: "missing",
      next_node_id: "end",
      condition: null,
      priority: 0,
      label: null,
    });
    const errors = validateSop(broken);
    expect(errors.some((error) => error.code === "duplicate_node_id")).toBe(true);
    expect(errors.some((error) => error.code === "missing_start")).toBe(true);
    expect(errors.some((error) => error.code === "missing_terminal")).toBe(true);
    expect(errors.some((error) => error.code === "edge_source_missing")).toBe(true);
  });

  it("存在从 start 不可达节点时报告 unreachable", () => {
    const broken = clone(card);
    broken.nodes.push({ ...broken.nodes[0], node_id: "orphan", name: "孤立节点" });
    const errors = validateSop(broken);
    expect(errors.some((error) => error.code === "unreachable" && error.nodeId === "orphan")).toBe(
      true,
    );
  });

  it("非终态节点无法到达 terminal 时报告 no_exit", () => {
    const broken = clone(card);
    broken.edges = broken.edges.filter((edge) => edge.next_node_id !== "end");
    const errors = validateSop(broken);
    expect(errors.some((error) => error.code === "no_exit")).toBe(true);
  });

  it("版本必须是三段式 semver", () => {
    const broken = clone(card);
    broken.version = "1.2";
    expect(validateSop(broken).some((error) => error.code === "invalid_version")).toBe(true);
  });
});

describe("sop-graph 布局与编辑辅助", () => {
  it("layoutSop 为每个节点提供有限坐标，且同一输入稳定", () => {
    const first = layoutSop(card);
    const second = layoutSop(card);
    expect(Object.keys(first).sort()).toEqual(["collect", "end", "start"]);
    for (const point of Object.values(first)) {
      expect(Number.isFinite(point.x)).toBe(true);
      expect(Number.isFinite(point.y)).toBe(true);
    }
    expect(second).toEqual(first);
  });

  it("节点位置写入 metadata.position 且不覆盖其他 metadata", () => {
    const next = setNodePosition(
      { ...card.nodes[0], metadata: { note: "保留", position: { x: 1, y: 2 } } },
      { x: 120, y: 80 },
    );
    expect(next.metadata).toEqual({ note: "保留", position: { x: 120, y: 80 } });
    expect(readPosition(next)).toEqual({ x: 120, y: 80 });
    expect(readPosition({ ...next, metadata: { position: { x: "bad", y: 0 } } })).toBeUndefined();
  });

  it("renameNode 同步重映射起点、终点与所有边", () => {
    const renamed = renameNode(card, "collect", "collect-product");
    expect(renamed.nodes.some((node) => node.node_id === "collect-product")).toBe(true);
    expect(renamed.edges.map((edge) => [edge.source_node_id, edge.next_node_id])).toEqual([
      ["start", "collect-product"],
      ["collect-product", "end"],
    ]);
  });

  it("buildFlowNodes / buildFlowEdges 输出画布所需字段", () => {
    const positions = layoutSop(card);
    const nodes = buildFlowNodes(card, positions);
    expect(nodes).toHaveLength(3);
    expect(nodes[0]).toMatchObject({ id: "start", type: "sop", width: 220 });
    expect(nodes[0].position).toEqual(positions.start);
    const edges = buildFlowEdges(card);
    expect(edges).toHaveLength(2);
    expect(edges[1]).toMatchObject({ source: "collect", target: "end", label: "完成" });
  });

  it("驳回/返工边渲染为虚线，普通边保持实线", () => {
    const withRework = clone(card);
    withRework.edges.push({
      source_node_id: "end",
      next_node_id: "collect",
      condition: null,
      priority: 0,
      label: "驳回",
    });
    const edges = buildFlowEdges(withRework);
    expect(edges[2].style?.strokeDasharray).toBe("4 3");
    expect(edges[2].type).toBe("rework");
    expect(edges[0].type).toBe("smoothstep");
    expect(edges[0].style?.strokeDasharray).toBeUndefined();
    expect(isReworkEdge(withRework.edges[2])).toBe(true);
    expect(
      isReworkEdge({ ...withRework.edges[2], label: "返工", condition: "质量不达标" }),
    ).toBe(true);
    expect(isReworkEdge(withRework.edges[0])).toBe(false);
  });
});
