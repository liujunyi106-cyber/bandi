import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import TeamPage from "@/pages/team";

const EMPLOYEES = [
  {
    employee_id: "emp-1",
    name: "选品专员",
    role_id: "sourcing",
    description: "负责供应商筛选与比价",
    status: "active",
    skills: ["price-compare"],
    llm_config: null,
  },
  {
    employee_id: "emp-2",
    name: "运营助理",
    role_id: "ops",
    description: "负责日常运营跟进",
    status: "paused",
    skills: [],
    llm_config: { default_model: "deepseek-chat" },
  },
];

const ROLES = [
  {
    id: "sourcing",
    name: "选品",
    reports_to: "lead",
    responsibility: "选品与比价",
    is_final_decider: false,
    tool_tags: ["web_search"],
  },
  {
    id: "lead",
    name: "主管",
    reports_to: null,
    responsibility: "统筹",
    is_final_decider: true,
    tool_tags: [],
  },
  {
    id: "ops",
    name: "运营",
    reports_to: "lead",
    responsibility: "运营跟进",
    is_final_decider: false,
    tool_tags: [],
  },
];

const ORG_TEAMS = [
  {
    team_id: "selection-team",
    name: "选品团队",
    role_count: 2,
    employee_count: 1,
    roles: [
      { role_id: "sourcing", name: "选品" },
      { role_id: "lead", name: "主管" },
    ],
  },
];

const WORK_ITEMS = {
  items: [
    {
      id: "wi-1",
      title: "整理供应商清单",
      phase: "running",
      assignee_role: "sourcing",
      column: "in_progress",
    },
  ],
  column_counts: { todo: 0, in_progress: 1, in_review: 0, done: 0 },
};

const PROFILE = {
  profile: {
    profile_id: "emp-1",
    employee_id: "emp-1",
    name: "选品专员",
    description: "负责供应商筛选与比价",
    persona_prompt: "你是选品专员，先比价再给结论。",
    status: "active",
    resources: [{ resource_type: "general_skill", resource_id: "price-compare", status: "active" }],
    metadata: {},
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-02T00:00:00Z",
  },
};

function json(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function stubTeamFetch(
  options: {
    employees?: unknown[];
    pending?: unknown[];
    orgTeams?: unknown[];
    onDissolve?: () => void;
  } = {},
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/company/employees/") && url.endsWith("/profile")) return json(PROFILE);
      if (url.includes("/company/employees/") && url.endsWith("/knowledge"))
        return json({ employee_id: "emp-1", bases: [] });
      if (url.includes("/memory/pending")) return json({ pending: options.pending ?? [] });
      if (url.includes("/company/employees"))
        return json({ employees: options.employees ?? EMPLOYEES });
      if (url.includes("/company/roles")) return json({ roles: ROLES });
      if (url.includes("/company/org-teams/") && (init?.method ?? "GET") === "DELETE") {
        options.onDissolve?.();
        return json({ dissolved: true });
      }
      if (url.includes("/company/org-teams"))
        return json({ teams: options.orgTeams ?? ORG_TEAMS });
      if (url.includes("/company/teams")) return json({ teams: [] });
      if (url.includes("/company/work-items")) return json(WORK_ITEMS);
      if (url.includes("/skills")) return json({ skills: [] });
      if (url.includes("/tools/meta")) return json([]);
      if (url.includes("/settings/model"))
        return json({
          current_model: "deepseek-chat",
          has_api_key: true,
          available_models: [],
          effective_model: "deepseek-chat",
        });
      return json({});
    }),
  );
}

function renderPage() {
  return render(
    <MemoryRouter>
      <TeamPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TeamPage 团队页", () => {
  it("加载员工后渲染卡片、岗位与统计", async () => {
    stubTeamFetch();
    renderPage();

    expect(await screen.findByText("选品专员")).toBeInTheDocument();
    expect(screen.getByText("运营助理")).toBeInTheDocument();
    expect(screen.getByText(/模型 跟随全局/)).toBeInTheDocument();
    expect(screen.getByText("员工总数")).toBeInTheDocument();
  });

  it("搜索按姓名/岗位/团队过滤卡片", async () => {
    stubTeamFetch();
    const user = userEvent.setup();
    renderPage();

    await screen.findByText("选品专员");
    await user.type(screen.getByLabelText("搜索员工 / 岗位 / 团队"), "运营");

    await waitFor(() => {
      expect(screen.queryByText("选品专员")).not.toBeInTheDocument();
    });
    expect(screen.getByText("运营助理")).toBeInTheDocument();
  });

  it("点击卡片打开档案并切到状态与组织 Tab", async () => {
    stubTeamFetch();
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId("employee-card-emp-1"));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("岗位人设")).toBeInTheDocument();
    expect(within(dialog).getByDisplayValue("负责供应商筛选与比价")).toBeInTheDocument();

    await user.click(within(dialog).getByRole("tab", { name: "组织" }));
    expect(within(dialog).queryByText("当前任务")).not.toBeInTheDocument();
    expect(within(dialog).getByText("汇报对象")).toBeInTheDocument();
    expect(within(dialog).getAllByText("主管").length).toBeGreaterThan(0);
  });

  it("空员工时展示示例团队与添加员工两个入口", async () => {
    stubTeamFetch({ employees: [] });
    renderPage();

    expect(await screen.findByText("还没有数字员工")).toBeInTheDocument();
    const emptyState = screen.getByTestId("team-empty-state");
    expect(
      within(emptyState).getByRole("button", { name: "一键体验示例团队" }),
    ).toBeInTheDocument();
    expect(within(emptyState).getByRole("button", { name: "添加员工" })).toBeInTheDocument();
  });

  it("删除员工确认弹窗：文案明确不可恢复", async () => {
    stubTeamFetch();
    const user = userEvent.setup();
    renderPage();

    const card = await screen.findByTestId("employee-card-emp-1");
    await user.click(within(card).getByRole("button", { name: /更多/ }));
    await user.click(await screen.findByText("删除员工"));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByRole("heading", { name: "删除员工" })).toBeInTheDocument();
    expect(within(dialog).getByText(/不可恢复/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "取消" }));
  });

  it("团队下拉解散：确认后团队移除并回落到无团队态", async () => {
    const teams = [...ORG_TEAMS];
    stubTeamFetch({
      orgTeams: teams,
      onDissolve: () => teams.splice(0, teams.length),
    });
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByText("组织编排"));
    await user.click(await screen.findByTestId("team-picker-trigger"));
    await user.click(await screen.findByTestId("team-dissolve-selection-team"));

    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: /解散/ }));

    await waitFor(() => {
      expect(screen.getByTestId("team-picker-trigger")).toHaveTextContent("团队管理");
    });
  });
});
