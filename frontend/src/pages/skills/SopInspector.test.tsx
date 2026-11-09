import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { SopCard } from "@/lib/sop-graph";
import { SopInspector } from "@/pages/skills/SopInspector";

const card = {
  skill_id: "sop-test",
  name: "测试 SOP",
  version: "1.2.0",
  business_domain: "电商",
  description: "测试用 SOP",
  enabled: true,
  trigger_intents: [],
  user_utterance_examples: [],
  goal: ["完成测试"],
  required_info: [],
  slot_filling_policy: {},
  response_rules: [],
  interruption_policy: {},
  start_node_id: "start",
  terminal_node_ids: ["end"],
  nodes: [
    {
      node_id: "start",
      name: "开始",
      type: "start",
      instruction: "接收请求",
      optional: false,
      condition: null,
      expected_user_info: [],
      allowed_actions: [],
      knowledge_scope: {},
      retry_policy: {},
      metadata: {},
    },
    {
      node_id: "end",
      name: "结束",
      type: "terminal",
      instruction: "完成",
      optional: false,
      condition: null,
      expected_user_info: [],
      allowed_actions: [],
      knowledge_scope: {},
      retry_policy: {},
      metadata: {},
    },
  ],
  edges: [
    {
      source_node_id: "start",
      next_node_id: "end",
      condition: null,
      priority: 0,
      label: "通过",
    },
  ],
} as unknown as SopCard;

function renderInspector(
  selected: Parameters<typeof SopInspector>[0]["selected"],
): {
  onChangeCard: ReturnType<typeof vi.fn>;
  onChangeNode: ReturnType<typeof vi.fn>;
  onChangeEdge: ReturnType<typeof vi.fn>;
  onDeleteNode: ReturnType<typeof vi.fn>;
  onDeleteEdge: ReturnType<typeof vi.fn>;
  onRenameNode: ReturnType<typeof vi.fn>;
  onDuplicateNode: ReturnType<typeof vi.fn>;
  onSelectEdge: ReturnType<typeof vi.fn>;
  onSaveWithPatch: ReturnType<typeof vi.fn>;
} {
  const onChangeCard = vi.fn();
  const onChangeNode = vi.fn();
  const onChangeEdge = vi.fn();
  const onDeleteNode = vi.fn();
  const onDeleteEdge = vi.fn();
  const onRenameNode = vi.fn(() => true);
  const onDuplicateNode = vi.fn();
  const onSelectEdge = vi.fn();
  const onSaveWithPatch = vi.fn();
  render(
    <SopInspector
      card={card}
      selected={selected}
      onChangeCard={onChangeCard}
      onChangeNode={onChangeNode}
      onChangeEdge={onChangeEdge}
      onDeleteNode={onDeleteNode}
      onDeleteEdge={onDeleteEdge}
      onRenameNode={onRenameNode}
      onDuplicateNode={onDuplicateNode}
      onSelectEdge={onSelectEdge}
      onSaveWithPatch={onSaveWithPatch}
    />,
  );
  return {
    onChangeCard,
    onChangeNode,
    onChangeEdge,
    onDeleteNode,
    onDeleteEdge,
    onRenameNode,
    onDuplicateNode,
    onSelectEdge,
    onSaveWithPatch,
  };
}

describe("SopInspector", () => {
  it("无选中时展示 SOP 设置并提交卡级字段修改", () => {
    const { onChangeCard } = renderInspector(null);
    expect(screen.getByText("SOP 设置")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("版本"), { target: { value: "1.3.0" } });
    expect(onChangeCard).toHaveBeenCalledWith({ version: "1.3.0" });
  });

  it("选中节点时提交节点字段修改，开始节点禁止删除", () => {
    const { onChangeNode } = renderInspector({ kind: "node", id: "start" });
    expect(screen.getByText("节点属性")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("名称", { exact: true }), {
      target: { value: "新开始" },
    });
    expect(onChangeNode).toHaveBeenCalledWith("start", { name: "新开始" });
    expect(screen.getByRole("button", { name: "删除节点" })).toBeDisabled();
  });

  it("选中连线时展示端点并提交标签修改", () => {
    const { onChangeEdge } = renderInspector({ kind: "edge", index: 0 });
    expect(screen.getByText("连线属性")).toBeInTheDocument();
    expect(screen.getByText("start → end")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("转移标签"), { target: { value: "驳回" } });
    expect(onChangeEdge).toHaveBeenCalledWith(0, { label: "驳回" });
  });

  it("节点 ID 失焦时提交重命名，复制按钮提交复制", () => {
    const { onRenameNode, onDuplicateNode } = renderInspector({ kind: "node", id: "start" });
    const nodeId = screen.getByLabelText("节点 ID");
    fireEvent.change(nodeId, { target: { value: "begin" } });
    fireEvent.blur(nodeId);
    expect(onRenameNode).toHaveBeenCalledWith("start", "begin");
    fireEvent.click(screen.getByRole("button", { name: "复制节点" }));
    expect(onDuplicateNode).toHaveBeenCalledWith("start");
  });

  it("节点出边列表可定位到连线", () => {
    const { onSelectEdge } = renderInspector({ kind: "node", id: "start" });
    fireEvent.click(screen.getByTestId("node-edge-0"));
    expect(onSelectEdge).toHaveBeenCalledWith(0);
  });

  it("卡级设置提供保存并升 patch", () => {
    const { onSaveWithPatch } = renderInspector(null);
    fireEvent.click(screen.getByRole("button", { name: "保存并升 patch" }));
    expect(onSaveWithPatch).toHaveBeenCalledTimes(1);
  });
});
