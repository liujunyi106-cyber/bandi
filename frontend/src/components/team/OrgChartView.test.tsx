import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RoleNodeBody, type RoleNodeData } from "@/components/team/OrgChartView";

function roleData(overrides: Partial<RoleNodeData> = {}): RoleNodeData {
  return {
    name: "产品设计师",
    roleId: "designer",
    teamName: "设计组",
    memberCount: 1,
    finalDecider: false,
    members: [],
    ...overrides,
  };
}

describe("OrgChart RoleNodeBody", () => {
  it("有员工时使用员工头像（与员工画廊同源），不再是角色首字灰块", () => {
    render(
      <RoleNodeBody
        data={roleData({
          members: [
            {
              name: "小美",
              metadata: { avatar: { kind: "tone", tone: 3, shape: "classic" } },
            },
          ],
        })}
      />,
    );
    const avatar = screen.getByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "1");
    expect(avatar).toHaveTextContent("小");
    expect(screen.getByText("产品设计师")).toBeInTheDocument();
  });

  it("无员工时回退角色首字", () => {
    render(<RoleNodeBody data={roleData({ memberCount: 0 })} />);
    const avatar = screen.getByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "0");
    expect(avatar).toHaveTextContent("产");
  });

  it("传入多员工时最多展示 4 个", () => {
    render(
      <RoleNodeBody
        data={roleData({
          memberCount: 5,
          members: [{ name: "甲" }, { name: "乙" }, { name: "丙" }, { name: "丁" }, { name: "戊" }],
        })}
      />,
    );
    expect(screen.getByTestId("team-avatar")).toHaveAttribute("data-count", "4");
    expect(screen.getAllByTestId("team-avatar-cell")).toHaveLength(4);
  });
});
