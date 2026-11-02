import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SopEditor } from "@/pages/skills/SopEditor";

const mocks = vi.hoisted(() => ({
  detail: vi.fn(),
  save: vi.fn(),
  setEnabled: vi.fn(),
}));

vi.mock("@/services/resources", () => ({
  resourcesApi: {
    detail: mocks.detail,
    save: mocks.save,
    setEnabled: mocks.setEnabled,
  },
}));

vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  return {
    ...actual,
    ReactFlow: ({
      nodes,
      onNodeClick,
      onPaneClick,
    }: {
      nodes: Array<{ id: string; data: { node: { name: string } } }>;
      onNodeClick?: (event: unknown, node: unknown) => void;
      onPaneClick?: () => void;
    }) => (
      <div data-testid="flow">
        <button type="button" data-testid="flow-pane" onClick={() => onPaneClick?.()} />
        {nodes.map((node) => (
          <button
            key={node.id}
            type="button"
            data-testid={`flow-node-${node.id}`}
            onClick={() => onNodeClick?.({}, node)}
          >
            {node.data.node.name}
          </button>
        ))}
      </div>
    ),
    Background: () => null,
    Controls: () => null,
    MiniMap: () => null,
  };
});

const CONTENT = JSON.stringify({
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
    },
    {
      node_id: "end",
      name: "结束",
      type: "terminal",
      instruction: "完成",
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
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.detail.mockResolvedValue({
    item: { id: "sop-test", kind: "sop", title: "测试 SOP" },
    content: CONTENT,
  });
  mocks.save.mockResolvedValue({ ok: true, item: {} });
  mocks.setEnabled.mockResolvedValue({ ok: true, item: {} });
});

describe("SopEditor", () => {
  it("加载 SOP 并渲染画布节点", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    expect(screen.getByTestId("flow-node-end")).toBeInTheDocument();
    expect(screen.getByTestId("flow-node-start")).toHaveTextContent("开始");
    expect(mocks.detail).toHaveBeenCalledWith("sop", "sop-test");
  });

  it("编辑节点名称后保存，提交更新后的 JSON", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    fireEvent.click(screen.getByTestId("flow-node-start"));
    fireEvent.change(screen.getByLabelText("名称", { exact: true }), {
      target: { value: "开始复核" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledTimes(1));
    const [, , content] = mocks.save.mock.calls[0];
    expect(JSON.parse(content).nodes[0].name).toBe("开始复核");
  });

  it("卡级版本非法时展示校验错误并阻止保存", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    fireEvent.click(screen.getByTestId("flow-pane"));
    expect(screen.getByText("SOP 设置")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("版本"), { target: { value: "1.2" } });
    fireEvent.click(screen.getByRole("button", { name: "校验" }));
    expect(screen.getAllByText(/版本必须是三段式 semver/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(mocks.save).not.toHaveBeenCalled();
  });

  it("节点 ID 重命名同步边与 start_node_id", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    fireEvent.click(screen.getByTestId("flow-node-start"));
    fireEvent.change(screen.getByLabelText("节点 ID"), { target: { value: "begin" } });
    fireEvent.blur(screen.getByLabelText("节点 ID"));
    await screen.findByTestId("flow-node-begin");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledTimes(1));
    const payload = JSON.parse(mocks.save.mock.calls[0][2]);
    expect(payload.start_node_id).toBe("begin");
    expect(payload.nodes[0].node_id).toBe("begin");
    expect(payload.edges[0].source_node_id).toBe("begin");
  });

  it("复制节点新增独立节点，未连线时保存被校验阻断", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    fireEvent.click(screen.getByTestId("flow-node-start"));
    fireEvent.click(screen.getByRole("button", { name: "复制节点" }));
    await screen.findByTestId("flow-node-start_copy");
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(mocks.save).not.toHaveBeenCalled();
    expect(screen.getAllByText(/不可达|无法到达/).length).toBeGreaterThan(0);
  });

  it("保存并升 patch 写入新版本号", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    fireEvent.click(screen.getByTestId("flow-pane"));
    fireEvent.click(screen.getByRole("button", { name: "保存并升 patch" }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledTimes(1));
    expect(JSON.parse(mocks.save.mock.calls[0][2]).version).toBe("1.2.1");
  });

  it("节点出边列表可选中并在右侧删除连线", async () => {
    render(<SopEditor resourceId="sop-test" onBack={vi.fn()} />);
    await screen.findByTestId("flow-node-start");
    fireEvent.click(screen.getByTestId("flow-node-start"));
    fireEvent.click(screen.getByTestId("node-edge-0"));
    expect(screen.getByText("连线属性")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "删除连线" }));
    expect(screen.getByText("SOP 设置")).toBeInTheDocument();
  });
});

