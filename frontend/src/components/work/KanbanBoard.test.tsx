import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { KanbanBoard } from "@/components/work/KanbanBoard";
import type { WorkItemSummary } from "@/services/company";

function item(id: string, phase: string, extra: Partial<WorkItemSummary> = {}): WorkItemSummary {
  return {
    id,
    title: `任务 ${id}`,
    phase,
    assignee_role: "产品设计师",
    updated_at: "2026-10-27T09:00:00+00:00",
    ...extra,
  };
}

function renderBoard(
  items: WorkItemSummary[],
  overrides: Partial<Parameters<typeof KanbanBoard>[0]> = {},
) {
  const props = {
    items,
    expandedId: null,
    onToggle: vi.fn(),
    onAction: vi.fn(),
    ...overrides,
  };
  return { ...render(<KanbanBoard {...props} />), props };
}

describe("KanbanBoard", () => {
  const items = [
    item("t_spec", "queued", { title: "需求规格" }),
    item("t_plan", "queued", { title: "视觉稿", mode_gate: "plan" }),
    item("t_run", "running", { title: "前端实现" }),
    item("t_review", "awaiting_human", { title: "验收评审" }),
    item("t_failed", "failed", { title: "排期草案" }),
  ];

  it("四列都在，计划卡与普通待办都落待办、失败落已完成", () => {
    renderBoard(items);

    const todo = screen.getByTestId("kanban-column-todo");
    const done = screen.getByTestId("kanban-column-done");
    expect(within(todo).getByText("视觉稿")).toBeInTheDocument();
    expect(within(todo).getByText("需求规格")).toBeInTheDocument();
    expect(within(done).getByText("排期草案")).toBeInTheDocument();
    expect(within(done).getByText("失败")).toBeInTheDocument();
    expect(screen.getByTestId("kanban-count-todo")).toHaveTextContent("2");
    expect(screen.getByTestId("kanban-count-done")).toHaveTextContent("1");
  });

  it("卡片只显示相位徽标，不再有「计划」标记", () => {
    renderBoard(items);
    expect(screen.queryByTestId("work-plan-badge-t_plan")).toBeNull();
    expect(screen.getAllByText("待排").length).toBe(2);
  });

  it("空列不渲染占位文案", () => {
    renderBoard([item("t_spec", "queued")]);
    const review = screen.getByTestId("kanban-column-in_review");
    expect(review).toBeInTheDocument();
    expect(within(review).queryByText("这一列还没有卡")).toBeNull();
    expect(within(review).queryByText("工作项会按阶段自动落列")).toBeNull();
  });

  it("点卡片回传 id（原地展开，不开抽屉）", async () => {
    const { props } = renderBoard(items);
    await userEvent.click(screen.getByTestId("work-item-t_review"));
    expect(props.onToggle).toHaveBeenCalledWith("t_review");
  });

  it("破坏者：带 mode_gate 的卡也没有「批准」入口（看板不做规划审批）", () => {
    renderBoard(items, { expandedId: "t_plan" });
    expect(screen.queryByTestId("work-approve-t_plan")).toBeNull();
    expect(screen.getByText(/暂无可用动作/)).toBeInTheDocument();
  });

  it("破坏者：失败卡展开后没有任何动作按钮", () => {
    renderBoard(items, { expandedId: "t_failed" });
    expect(screen.queryByTestId("work-retry-t_failed")).toBeNull();
    expect(screen.queryByTestId("work-abort-t_failed")).toBeNull();
    expect(screen.getByText(/暂无可用动作/)).toBeInTheDocument();
  });

  it("待审卡展开后提供「通过 / 返工」动作，返工必须填写要点", async () => {
    const { props } = renderBoard(items, { expandedId: "t_review" });
    const input = screen.getByTestId("work-review-input-t_review");
    const rework = screen.getByTestId("work-review-rework-t_review");
    expect(rework).toBeDisabled();

    await userEvent.type(input, "补充成本口径");
    expect(rework).toBeEnabled();
    await userEvent.click(rework);
    expect(props.onAction).toHaveBeenCalledWith("t_review", "review-rework", "补充成本口径");

    await userEvent.click(screen.getByTestId("work-review-pass-t_review"));
    expect(props.onAction).toHaveBeenCalledWith("t_review", "review-approve", "补充成本口径");
  });

  it("已完成列默认只铺 20 张，其余「载入更早」", async () => {
    const many = Array.from({ length: 25 }, (_, index) =>
      item(`d_${index}`, "approved", { title: `历史 ${index}` }),
    );
    renderBoard(many);
    expect(screen.getByTestId("kanban-count-done")).toHaveTextContent("25");
    expect(screen.getAllByTestId(/^work-item-d_/).length).toBe(20);

    await userEvent.click(screen.getByTestId("kanban-done-more"));
    expect(screen.getAllByTestId(/^work-item-d_/).length).toBe(25);
  });
});
