/**
 * 功能：右栏员工视图测试（群聊列表展开 / 单聊直出 / 助理卡 / 自动展开）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EmployeesView } from "@/components/chat/panel/EmployeesView";
import { companyApi } from "@/services/company";
import { panelApi } from "@/services/panel";
import { useChatStore } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      employees: vi.fn(async () => ({
        employees: [
          // 档案技能绑定按员工区分（右栏技能面只显示各自绑定——非全量）
          { employee_id: "e1", name: "小美", role_id: "dev", skills: ["报告术"] },
          { employee_id: "e2", name: "阿强", role_id: "ops", skills: [] },
        ],
      })),
      projects: vi.fn(async () => ({
        projects: [
          {
            project_id: "p1",
            name: "选品",
            root_dir: "",
            team_ids: ["t1"],
            created_at: "",
            updated_at: "",
            members: [
              {
                team_id: "t1",
                role_id: "dev",
                role_name: "开发",
                employee_id: "e1",
                employee_name: "小美",
                employee_status: "active",
              },
              {
                team_id: "t1",
                role_id: "ops",
                role_name: "运维",
                employee_id: "e2",
                employee_name: "阿强",
                employee_status: "active",
              },
              {
                team_id: "t1",
                role_id: "old",
                role_name: "离场",
                employee_id: "e3",
                employee_name: "旧人",
                employee_status: "inactive",
              },
            ],
          },
        ],
      })),
      workItems: vi.fn(async () => ({ items: [], column_counts: {} })),
      threads: vi.fn(async () => ({
        threads: [
          {
            thread_id: "th-ops",
            session_id: "dm-ops",
            parent_task_id: null,
            participants: ["user", "ops"],
            created_at: "",
            message_count: 1,
            kind: "dm",
          },
        ],
      })),
    },
  };
});

vi.mock("@/services/panel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/panel")>();
  return {
    ...actual,
    panelApi: {
      ...actual.panelApi,
      skills: vi.fn(async () => ({
        skills: [{ name: "报告术", tags: [], team_id: "", always: false }],
      })),
      sop: vi.fn(async (sessionId: string) => ({ session_id: sessionId, sop: null })),
      costDetail: vi.fn(async (sessionId: string) => ({
        session_id: sessionId,
        total_tokens: 100,
        total_cost_cny: 0.01,
        per_model: {},
        tool_stats: {},
      })),
      employeeKnowledge: vi.fn(async (employeeId: string) => ({
        employee_id: employeeId,
        bases: [{ base_id: "kb:dev", name: "开发知识", project_id: "", document_count: 2 }],
      })),
      // 项目口径（单聊态数据面）
      projectUsage: vi.fn(async (projectId: string, role = "") => ({
        project_id: projectId,
        role,
        total_tokens: 2222,
        total_cost_cny: 0.1,
        requests: 1,
      })),
      projectArtifacts: vi.fn(async (projectId: string, role = "") => ({
        project_id: projectId,
        role,
        artifacts: [
          {
            id: "pa-ops",
            session_id: `role-session::${projectId}::scope::${role}::e2`,
            path: "运维报告.md",
            tool_name: "write",
          },
        ],
        count: 1,
      })),
      employeeProfile: vi.fn(async (employeeId: string) => ({
        profile: {
          profile_id: "pf-1",
          employee_id: employeeId,
          name: "x",
          resources: [{ resource_type: "skill", resource_id: "ops-skill", status: "active" }],
        },
      })),
    },
  };
});

function renderView(entry: string) {
  // 模拟 SessionPanel 作用域口径：员工单聊态 sessionId 为空（生产 scope=employee → ""）
  const sessionId = entry.includes("view=dm") ? "" : "s1";
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <EmployeesView sessionId={sessionId} />
    </MemoryRouter>,
  );
}

describe("EmployeesView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ activeProjectId: "", activeSessionId: null, sessions: [] });
    usePanelStore.setState({ open: true, view: "employees", expandRoleId: "" });
  });

  it("项目会话：项目在职员工竖排单列（离场不显示），单击展开详细数据", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ activeProjectId: "p1" });
    renderView("/?project=p1");
    // 在职逐个排列（dev/ops）；离场成员 old 不渲染
    expect(await screen.findByTestId("panel-employee-dev")).toBeInTheDocument();
    expect(screen.getByTestId("panel-employee-ops")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-employee-old")).not.toBeInTheDocument();
    // 点击展开 → 摘要格 + 技能 + 知识库 + 项目口径
    await user.click(screen.getByTestId("panel-employee-open-dev"));
    expect(await screen.findByTestId("employee-card-details")).toBeInTheDocument();
    expect(await screen.findByText("报告术")).toBeInTheDocument();
    expect(await screen.findByText("开发知识")).toBeInTheDocument();
    expect(panelApi.projectUsage).toHaveBeenCalledWith("p1", "dev");
    // 员工技能按档案绑定区分：未绑定技能的员工不显示他人技能（破坏者视角）
    await user.click(screen.getByTestId("panel-employee-open-ops"));
    const skillBlocks = await screen.findAllByTestId("employee-skills");
    expect(skillBlocks[skillBlocks.length - 1]).not.toHaveTextContent("报告术");
  });

  it("单聊：直出目标员工 + 会话口径 token（直聊线程，不落项目累计）", async () => {
    renderView("/chat?view=dm&target=ops&project=p1");
    expect(await screen.findByTestId("panel-employee-ops")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-employee-dev")).not.toBeInTheDocument();
    await waitFor(() => expect(panelApi.costDetail).toHaveBeenCalledWith("dm-ops"));
    expect(panelApi.sop).toHaveBeenCalledWith("dm-ops");
    // 项目累计 token 不得覆盖单聊会话 token
    expect(panelApi.projectUsage).not.toHaveBeenCalled();
    // 项目产物/绑定卡数仍按项目口径
    expect(panelApi.projectArtifacts).toHaveBeenCalledWith("p1", "ops");
    expect(panelApi.employeeProfile).toHaveBeenCalledWith("ops");
    expect(await screen.findByTestId("employee-artifacts")).toHaveTextContent("运维报告.md");
  });

  it("G3：expandRoleId 预置 → 自动展开该员工", async () => {
    usePanelStore.setState({ expandRoleId: "ops" });
    useChatStore.setState({ activeProjectId: "p1" });
    renderView("/?project=p1");
    expect(await screen.findByTestId("panel-employee-ops")).toBeInTheDocument();
    // 展开态：CardDetails 出现
    expect(await screen.findByTestId("employee-card-details")).toBeInTheDocument();
  });

  it("普通会话：助理卡 =「助理」", async () => {
    renderView("/");
    expect(await screen.findByTestId("panel-employee-secretary")).toBeInTheDocument();
    expect(screen.getByText("助理")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
    expect(screen.queryByText("秘书")).not.toBeInTheDocument();
  });

  it("无员工（默认仅助理接待）：普通会话助理卡显示「助理」", async () => {
    vi.mocked(companyApi.employees).mockResolvedValueOnce({ employees: [] });
    renderView("/");
    expect(await screen.findByTestId("panel-employee-secretary")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
  });

  it("助理会话：助理卡显示「助理」（2026-10-10 秘书→助理）", async () => {
    useChatStore.setState({
      activeSessionId: "s-secretary",
      sessions: [
        {
          id: "s-secretary",
          title: "助理会话",
          created_at: "2026-09-29T01:00:00",
          message_count: 1,
          project_id: "p1",
          origin: "secretary",
        },
      ],
    });
    renderView("/");
    expect(await screen.findByTestId("panel-employee-secretary")).toBeInTheDocument();
    expect(screen.getByText("助理")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
    expect(screen.queryByText("秘书")).not.toBeInTheDocument();
  });

  it("团队会话（项目上下文）：显示在职员工竖排列表而非助理卡", async () => {
    useChatStore.setState({ activeProjectId: "p1" });
    renderView("/");
    expect(await screen.findByTestId("panel-employee-dev")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-employee-secretary")).not.toBeInTheDocument();
  });

  it("助理卡可收起/展开", async () => {
    const user = userEvent.setup();
    renderView("/");
    expect(await screen.findByTestId("employee-card-details")).toBeInTheDocument();
    await user.click(screen.getByTestId("panel-employee-open-secretary"));
    expect(screen.queryByTestId("employee-card-details")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("panel-employee-open-secretary"));
    expect(await screen.findByTestId("employee-card-details")).toBeInTheDocument();
  });
});
