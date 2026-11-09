import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TemplateGallery } from "@/components/team/TemplateGallery";
import type { OrgTeamSummary, TeamTemplateSummary } from "@/services/company";

const instantiate = vi.fn();
const hire = vi.fn();

vi.mock("@/services/team", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/team")>();
  return {
    ...actual,
    teamApi: {
      ...actual.teamApi,
      talentTemplates: vi.fn().mockResolvedValue({
        templates: [
          { name: "writer", category: "content", description: "文案撰写" },
          { name: "reviewer", category: "quality", description: "质量把关" },
        ],
      }),
      instantiateTeam: (...args: unknown[]) => instantiate(...args),
      hireTalent: (...args: unknown[]) => hire(...args),
    },
  };
});

const TEMPLATES: TeamTemplateSummary[] = [
  {
    template_id: "selection-team",
    name: "选品团队",
    description: "调研 → 选品 → 上架评审",
    role_count: 2,
    roles: [
      { id: "lead", name: "选品经理", reports_to: null },
      { id: "research", name: "市场调研", reports_to: "lead" },
    ],
  },
  {
    template_id: "product-team",
    name: "产品开发团队",
    description: "需求 → 设计 → 开发 → 验收",
    role_count: 1,
    created_at: "2026-10-01T10:00:00Z",
    roles: [{ id: "pm", name: "产品经理", reports_to: null }],
  },
];

function renderGallery(orgTeams: OrgTeamSummary[] = []) {
  return render(
    <TemplateGallery templates={TEMPLATES} orgTeams={orgTeams} onApplied={() => {}} />,
  );
}

describe("TemplateGallery", () => {
  it("默认展示团队模板：计数、内置/自定义徽标、角色 chips", () => {
    renderGallery();
    expect(screen.getByTestId("team-template-selection-team")).toHaveTextContent("选品团队");
    expect(screen.getByTestId("team-template-selection-team")).toHaveTextContent("2 角色");
    expect(screen.getByTestId("team-template-product-team")).toHaveTextContent("自定义");
    expect(screen.getByTestId("team-template-selection-team")).toHaveTextContent("内置");
    expect(screen.getByText("选品经理")).toBeInTheDocument();
  });

  it("切到人才模板后展示人才卡（团队卡不再渲染）", async () => {
    renderGallery();
    await userEvent.click(screen.getByTestId("template-tab-talent"));
    await waitFor(() => expect(screen.getByTestId("talent-template-writer")).toBeInTheDocument());
    expect(screen.getByTestId("talent-template-reviewer")).toBeInTheDocument();
    expect(screen.queryByTestId("team-template-selection-team")).toBeNull();
  });

  it("已有同名团队时默认 ID 自动加序号，避免一点确认就失败", async () => {
    instantiate.mockClear();
    instantiate.mockResolvedValue({ report: { created_employee_ids: ["a"] } });
    renderGallery([
      { team_id: "selection-team", name: "选品团队", role_count: 2, employee_count: 2, roles: [] },
    ]);

    await userEvent.click(
      screen.getByTestId("team-template-selection-team").querySelector("button")!,
    );
    await userEvent.click(screen.getByTestId("template-team-id-toggle"));
    const input = screen.getByTestId("template-team-id") as HTMLInputElement;
    expect(input.value).toBe("selection-team-2");

    await userEvent.click(screen.getByTestId("template-apply-confirm"));
    await waitFor(() =>
      expect(instantiate).toHaveBeenCalledWith("selection-team", "selection-team-2", "选品团队"),
    );
  });

  it("手动改成已存在的 ID → 提示同名并禁用确认（不发请求）", async () => {
    instantiate.mockClear();
    renderGallery([
      { team_id: "selection-team", name: "选品团队", role_count: 2, employee_count: 2, roles: [] },
    ]);

    await userEvent.click(
      screen.getByTestId("team-template-selection-team").querySelector("button")!,
    );
    await userEvent.click(screen.getByTestId("template-team-id-toggle"));
    const input = screen.getByTestId("template-team-id");
    await userEvent.clear(input);
    await userEvent.type(input, "selection-team");

    expect(screen.getByTestId("template-team-id-hint")).toHaveTextContent("已有同名团队");
    expect(screen.getByTestId("template-apply-confirm")).toBeDisabled();
    expect(instantiate).not.toHaveBeenCalled();
  });

  it("非法字符 → 提示只允许字母数字与 . _ - 且禁用确认", async () => {
    renderGallery();
    await userEvent.click(
      screen.getByTestId("team-template-selection-team").querySelector("button")!,
    );
    await userEvent.click(screen.getByTestId("template-team-id-toggle"));
    const input = screen.getByTestId("template-team-id");
    await userEvent.clear(input);
    await userEvent.type(input, "选品 团队");

    expect(screen.getByTestId("template-team-id-hint")).toHaveTextContent("只能使用");
    expect(screen.getByTestId("template-apply-confirm")).toBeDisabled();
  });

  it("默认团队名称 = 模板名，改名后按名称传后端", async () => {
    instantiate.mockClear();
    instantiate.mockResolvedValue({ report: { created_employee_ids: ["a"] } });
    renderGallery();

    await userEvent.click(
      screen.getByTestId("team-template-selection-team").querySelector("button")!,
    );
    const nameInput = screen.getByTestId("template-team-name") as HTMLInputElement;
    expect(nameInput.value).toBe("选品团队");

    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "我的选品组");
    await userEvent.click(screen.getByTestId("template-apply-confirm"));

    await waitFor(() =>
      expect(instantiate).toHaveBeenCalledWith("selection-team", "selection-team", "我的选品组"),
    );
  });

  it("改成新团队 ID 后按模板与团队 ID 调实例化", async () => {
    instantiate.mockClear();
    instantiate.mockResolvedValue({ report: { created_employee_ids: ["a", "b"] } });
    renderGallery([
      { team_id: "selection-team", name: "选品团队", role_count: 2, employee_count: 2, roles: [] },
    ]);

    await userEvent.click(
      screen.getByTestId("team-template-selection-team").querySelector("button")!,
    );
    await userEvent.click(screen.getByTestId("template-team-id-toggle"));
    const input = screen.getByTestId("template-team-id");
    await userEvent.clear(input);
    await userEvent.type(input, "selection-copy");
    await userEvent.click(screen.getByTestId("template-apply-confirm"));

    await waitFor(() =>
      expect(instantiate).toHaveBeenCalledWith("selection-team", "selection-copy", "选品团队"),
    );
  });
});
