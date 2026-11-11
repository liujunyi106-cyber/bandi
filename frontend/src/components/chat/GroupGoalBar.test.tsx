import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GroupGoalBar } from "@/components/chat/GroupGoalBar";
import type { GroupGoalSnapshot } from "@/services/company";

function goal(patch: Partial<GroupGoalSnapshot> = {}): GroupGoalSnapshot {
  return {
    goal_id: "g1",
    session_id: "s1",
    project_id: "p1",
    title: "群聊目标账本",
    status: "active",
    current: 2,
    total: 4,
    stage_title: "顶部目标条",
    owner_role_id: "lead",
    owner_name: "组长",
    revision: 3,
    updated_at: 1,
    ...patch,
  };
}

describe("GroupGoalBar", () => {
  it("renders nothing without a goal", () => {
    const { container } = render(<GroupGoalBar goal={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows title, status, progress and current stage", () => {
    render(<GroupGoalBar goal={goal()} />);
    expect(screen.getByTestId("group-goal-bar")).toBeInTheDocument();
    expect(screen.getByText("群聊目标账本")).toBeInTheDocument();
    expect(screen.getByTestId("group-goal-status")).toHaveTextContent("进行中");
    expect(screen.getByTestId("group-goal-progress")).toHaveTextContent("2 / 4");
    expect(screen.getByText(/顶部目标条/)).toBeInTheDocument();
  });

  it("keeps drafting compact without stage progress", () => {
    render(<GroupGoalBar goal={goal({ status: "drafting", current: 0, total: 0, title: "" })} />);
    expect(screen.getByText("正在对齐群聊目标")).toBeInTheDocument();
    expect(screen.getByTestId("group-goal-status")).toHaveTextContent("对齐中");
    expect(screen.queryByTestId("group-goal-progress")).not.toBeInTheDocument();
  });
});
