import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SopCreateDialog } from "@/pages/skills/SopCreateDialog";

const mocks = vi.hoisted(() => ({
  createSop: vi.fn(),
}));

vi.mock("@/services/resources", () => ({
  resourcesApi: {
    createSop: mocks.createSop,
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.createSop.mockResolvedValue({
    ok: true,
    item: { id: "new-sop", kind: "sop", title: "新建 SOP" },
  });
});

function renderDialog(onCreated = vi.fn(), onOpenChange = vi.fn()) {
  render(<SopCreateDialog open onOpenChange={onOpenChange} onCreated={onCreated} />);
  return { onCreated, onOpenChange };
}

describe("SopCreateDialog", () => {
  it("填写名称与标识后创建空白 SOP", async () => {
    const { onCreated, onOpenChange } = renderDialog();
    fireEvent.change(screen.getByLabelText("SOP 名称"), { target: { value: "新品上架" } });
    fireEvent.change(screen.getByLabelText("SOP 标识"), { target: { value: "new-launch" } });
    fireEvent.change(screen.getByLabelText("SOP 描述"), { target: { value: "从零开始" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并编辑" }));

    await waitFor(() => expect(mocks.createSop).toHaveBeenCalledTimes(1));
    const card = JSON.parse(mocks.createSop.mock.calls[0][0]);
    expect(card).toMatchObject({
      skill_id: "new-launch",
      name: "新品上架",
      version: "1.0.0",
      start_node_id: "start",
      terminal_node_ids: ["end"],
    });
    expect(card.nodes).toHaveLength(2);
    expect(card.edges).toHaveLength(1);
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("标识非法时阻止创建并提示", () => {
    renderDialog();
    fireEvent.change(screen.getByLabelText("SOP 名称"), { target: { value: "测试" } });
    fireEvent.change(screen.getByLabelText("SOP 标识"), { target: { value: "../escape" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并编辑" }));
    expect(screen.getByText(/标识只能包含/)).toBeInTheDocument();
    expect(mocks.createSop).not.toHaveBeenCalled();
  });
});
