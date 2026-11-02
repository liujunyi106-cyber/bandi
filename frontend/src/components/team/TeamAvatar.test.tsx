import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TeamAvatar } from "@/components/team/TeamAvatar";

const member = (name: string, metadata?: Record<string, unknown>) => ({ name, metadata });

describe("TeamAvatar", () => {
  it("无成员时显示方形图标回退（不再是圆形）", () => {
    render(<TeamAvatar members={[]} />);
    const avatar = screen.getByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "0");
    expect(avatar.className).toContain("rounded-md");
    expect(avatar.className).not.toContain("rounded-full");
    expect(avatar.querySelector("svg")).not.toBeNull();
    expect(screen.queryByTestId("team-avatar-cell")).not.toBeInTheDocument();
  });

  it("无成员且 fallback=initial 时显示角色首字", () => {
    render(<TeamAvatar members={[]} name="产品设计师" fallback="initial" />);
    expect(screen.getByTestId("team-avatar")).toHaveTextContent("产");
  });

  it("1 名员工满铺，保留员工头像样式（tone）", () => {
    render(
      <TeamAvatar
        members={[member("小美", { avatar: { kind: "tone", tone: 2, shape: "classic" } })]}
      />,
    );
    const avatar = screen.getByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "1");
    expect(avatar).toHaveTextContent("小");
    expect(screen.queryByTestId("team-avatar-cell")).not.toBeInTheDocument();
  });

  it("2 名员工左右并列并垂直居中", () => {
    render(<TeamAvatar members={[member("小美"), member("小林")]} />);
    const avatar = screen.getByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "2");
    expect(avatar.className).toContain("items-center");
    const cells = screen.getAllByTestId("team-avatar-cell");
    expect(cells).toHaveLength(2);
    expect(cells[0].className).toContain("size-[10px]");
  });

  it("3 名员工上一下二、上下居中对齐", () => {
    render(<TeamAvatar members={[member("甲"), member("乙"), member("丙")]} />);
    const cells = screen.getAllByTestId("team-avatar-cell");
    expect(cells).toHaveLength(3);
    expect(screen.getByTestId("team-avatar-top")).toContainElement(cells[0]);
    const bottom = screen.getByTestId("team-avatar-bottom");
    expect(bottom).toContainElement(cells[1]);
    expect(bottom).toContainElement(cells[2]);
  });

  it("最多展示 4 名员工（第 5 名截断），4 人内部尺寸缩小", () => {
    render(
      <TeamAvatar
        members={[member("甲"), member("乙"), member("丙"), member("丁"), member("戊")]}
      />,
    );
    const avatar = screen.getByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "4");
    const cells = screen.getAllByTestId("team-avatar-cell");
    expect(cells).toHaveLength(4);
    expect(cells[0].className).toContain("size-[9px]");
    expect(avatar).not.toHaveTextContent("戊");
  });

  it("上传头像按员工 metadata 渲染图片", () => {
    render(
      <TeamAvatar
        members={[
          member("小美", {
            avatar: { kind: "upload", url: "/api/company/uploads/u-1", tone: 0, shape: "classic" },
          }),
        ]}
      />,
    );
    expect(screen.getByTestId("team-avatar").querySelector("img")).toHaveAttribute(
      "src",
      "/api/company/uploads/u-1",
    );
  });
});
