/**
 * 功能：成员任务卡行为契约——
 * 标题「成员任务」/ 指派给员工名 / 【任务 #N】标题 + 折叠展开完整任务 /
 * 串行一块并行多块 / 状态标签。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MemberTaskCards } from "@/components/chat/MemberTaskCard";
import { useUiStore } from "@/stores/ui-store";
import type { WorkItemSummary } from "@/services/company";

vi.mock("@/hooks/use-employees", () => ({
  useEmployees: () => ({
    employees: [
      { role_id: "analyst", name: "数据分析专家", title: "", status: "active" },
      { role_id: "designer", name: "设计专家", title: "", status: "active" },
    ],
    loading: false,
    error: null,
  }),
}));

function task(overrides: Partial<WorkItemSummary> = {}): WorkItemSummary {
  return {
    id: "t1",
    title: "美国宠物用品市场调研",
    phase: "RUNNING",
    assignee_role: "analyst",
    description: "完整任务说明：基于公开需求/竞争/供应证据比较 3 个候选方向……",
    ...overrides,
  };
}

describe("成员任务卡", () => {
  beforeEach(() => {
    useUiStore.setState({ locale: "zh" });
  });

  it("单卡：标题 + 指派给员工名 + 任务编号 + 默认展开", () => {
    render(<MemberTaskCards items={[task()]} />);
    expect(screen.getByTestId("member-task-cards")).toBeInTheDocument();
    expect(screen.getByText("成员任务")).toBeInTheDocument();
    expect(screen.getByText(/指派给/)).toHaveTextContent("数据分析专家");
    expect(screen.getByText(/【任务 1】/)).toBeInTheDocument();
    // 单卡默认展开：完整任务描述可见
    expect(screen.getByText(/完整任务说明/)).toBeInTheDocument();
  });

  it("折叠箭头：点击后收起完整任务（再点恢复）", async () => {
    const user = userEvent.setup();
    render(<MemberTaskCards items={[task()]} />);
    await user.click(screen.getByTestId("member-task-toggle-t1"));
    expect(screen.queryByText(/完整任务说明/)).not.toBeInTheDocument();
    await user.click(screen.getByTestId("member-task-toggle-t1"));
    expect(screen.getByText(/完整任务说明/)).toBeInTheDocument();
  });

  it("并行多卡：并列多块各自独立折叠 + 编号递增", () => {
    render(
      <MemberTaskCards
        items={[
          task({ id: "t1", assignee_role: "analyst" }),
          task({ id: "t2", title: "概念图设计", assignee_role: "designer" }),
        ]}
      />,
    );
    expect(screen.getAllByTestId("member-task-cards")).toHaveLength(1);
    expect(screen.getByText(/【任务 1】/)).toBeInTheDocument();
    expect(screen.getByText(/【任务 2】/)).toBeInTheDocument();
    expect(screen.getByText("概念图设计", { exact: false })).toBeInTheDocument();
    expect(screen.getAllByText(/指派给/)).toHaveLength(2);
  });

  it("失败/取消优先于 column，不得显示成已完成", () => {
    render(
      <MemberTaskCards
        items={[
          task({ id: "t1", phase: "failed", column: "done" }),
          task({ id: "t2", phase: "cancelled", column: "done" }),
        ]}
      />,
    );
    expect(screen.getByTestId("member-task-status-t1")).toHaveTextContent("失败");
    expect(screen.getByTestId("member-task-status-t2")).toHaveTextContent("已取消");
  });

  it("状态标签：列口径映射（进行中/已完成）", () => {
    render(
      <MemberTaskCards
        items={[
          task({ id: "t1", phase: "running" }),
          task({ id: "t2", phase: "approved" }),
        ]}
      />,
    );
    expect(screen.getByTestId("member-task-status-t1")).toHaveTextContent("进行中");
    expect(screen.getByTestId("member-task-status-t2")).toHaveTextContent("已完成");
  });
});
