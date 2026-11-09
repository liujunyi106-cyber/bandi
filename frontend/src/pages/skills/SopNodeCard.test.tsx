import { render, screen } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";
import { describe, expect, it } from "vitest";

import type { SopNodeData } from "@/lib/sop-graph";
import { SopNodeCard } from "@/pages/skills/SopNodeCard";

function data(overrides: Partial<SopNodeData> = {}): SopNodeData {
  return {
    node: {
      node_id: "collect",
      name: "收集商品信息",
      type: "collect_info",
      instruction: "收集名称与卖点",
      optional: false,
      condition: null,
      expected_user_info: [],
      allowed_actions: [],
      knowledge_scope: {},
      retry_policy: {},
      metadata: {},
    },
    isStart: false,
    isTerminal: false,
    ...overrides,
  };
}

function renderNode(value: SopNodeData, selected = false) {
  render(
    <ReactFlowProvider>
      <SopNodeCard data={value} selected={selected} />
    </ReactFlowProvider>,
  );
}

describe("SopNodeCard", () => {
  it("展示节点名称、指令与可选标记", () => {
    renderNode(data({ node: { ...data().node, optional: true } }));
    expect(screen.getByText("收集商品信息")).toBeInTheDocument();
    expect(screen.getByText("收集名称与卖点")).toBeInTheDocument();
    expect(screen.getByText("可选")).toBeInTheDocument();
  });

  it("开始节点使用左侧强调色，选中态描边", () => {
    renderNode(data({ isStart: true }), true);
    const node = screen.getByTestId("sop-node-collect");
    expect(node.className).toContain("border-l-[3px]");
    expect(node.className).toContain("border-primary");
  });

  it("校验错误节点展示红框高亮", () => {
    renderNode(data({ error: true }));
    expect(screen.getByTestId("sop-node-collect").className).toContain("ring-2");
  });
});
