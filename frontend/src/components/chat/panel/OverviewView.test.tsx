/**
 * 功能：右栏概览视图测试（摘要卡 / 计划树 / 产物点击 / SOP 优先）。
 * .2：员工作用域 = 单员工卡（不发会话维度请求）。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewView } from "@/components/chat/panel/OverviewView";
import type { PanelScope } from "@/lib/panel-scope";
import { companyApi } from "@/services/company";
import { panelApi } from "@/services/panel";
import { sessionsApi } from "@/services/sessions";
import { useChatStore, type TaskLedgerItem } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

const SESSION_SCOPE: PanelScope = { kind: "session", key: "session:s1", sessionId: "s1" };
const EMPLOYEE_SCOPE: PanelScope = {
  kind: "employee",
  key: "employee:p1::dev",
  projectId: "p1",
  roleId: "dev",
};

vi.mock("@/services/panel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/panel")>();
  return {
    ...actual,
    panelApi: {
      ...actual.panelApi,
      skills: vi.fn(async () => ({ skills: [{ name: "s1" }, { name: "s2" }] })),
      sop: vi.fn(async () => ({ session_id: "s1", sop: null })),
      costDetail: vi.fn(async () => ({
        session_id: "s1",
        total_tokens: 12345,
        total_cost_cny: 0.42,
        per_model: {},
        tool_stats: {},
      })),
      employeeKnowledge: vi.fn(async (employeeId: string) => ({
        employee_id: employeeId,
        bases: [],
      })),
      // 项目口径（团队段 / 员工卡）
      projectUsage: vi.fn(async (projectId: string, role = "") => ({
        project_id: projectId,
        role,
        total_tokens: role ? 2222 : 88888,
        total_cost_cny: 0.5,
        requests: 3,
      })),
      projectArtifacts: vi.fn(async (projectId: string, role = "") => ({
        project_id: projectId,
        role,
        artifacts: [
          {
            id: "pa1",
            session_id: "role-session::p1::scope::dev::e1",
            path: "团队产物.md",
            tool_name: "write",
          },
        ],
        count: 1,
      })),
      employeeProfile: vi.fn(async (employeeId: string) => ({
        profile: {
          profile_id: "pf1",
          employee_id: employeeId,
          name: "小美",
          resources: [
            { resource_type: "skill", resource_id: "发货技能", status: "active" },
            { resource_type: "knowledge_base", resource_id: "kb:dev", status: "active" },
          ],
        },
      })),
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      artifacts: vi.fn(async () => ({
        session_id: "s1",
        artifacts: [
          {
            id: "a1",
            session_id: "s1",
            tool_name: "write",
            artifact_type: "file",
            path: "docs/out.md",
            hash: "",
          },
        ],
      })),
    },
  };
});

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      projects: vi.fn(async () => ({ projects: [] })),
      workItems: vi.fn(async () => ({ items: [], column_counts: {} })),
      threads: vi.fn(async () => ({ threads: [] })),
      employees: vi.fn(async () => ({ employees: [] })),
    },
  };
});

const tasks: TaskLedgerItem[] = [
  { id: "t1", title: "调研", content: "", status: "completed" },
  { id: "t2", title: "撰写", content: "", status: "in_progress" },
];

describe("OverviewView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ activeProjectId: "", tasksBySession: { s1: tasks } });
    usePanelStore.setState({
      open: true,
      view: "overview",
      taskFocusId: "",
      tabs: [],
      activeTabId: "",
      fileFocusPath: "",
    });
  });

  it("摘要卡：技能数（团队员工绑定合计）/ token 用量 / 任务进度 / 产物数", async () => {
    useChatStore.setState({ activeProjectId: "p1" });
    vi.mocked(companyApi.projects).mockResolvedValueOnce({
      projects: [
        {
          project_id: "p1",
          name: "选品",
          root_dir: "",
          team_ids: ["t1"],
          created_at: "",
          updated_at: "",
          members: [
            { team_id: "t1", role_id: "dev", role_name: "开发" },
            { team_id: "t1", role_id: "qa", role_name: "测试" },
          ],
        },
      ],
    } as never);
    vi.mocked(companyApi.employees).mockResolvedValueOnce({
      employees: [
        { employee_id: "e1", name: "开发", role_id: "dev", skills: ["s1", "s2"] },
        { employee_id: "e2", name: "测试", role_id: "qa", skills: ["s2", "s3"] },
      ],
    } as never);
    render(<OverviewView scope={SESSION_SCOPE} />);
    // 绑定技能去重合计 = {s1,s2,s3} = 3（此前显示全局库存）
    expect(await screen.findByText("3")).toBeInTheDocument();
    expect(await screen.findByText("12.3k")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
  });

  it("G5：计划树 Todo 平铺——点击任务 → 概览定位", async () => {
    render(<OverviewView scope={SESSION_SCOPE} />);
    fireEvent.click(await screen.findByTestId("panel-task-t2"));
    expect(usePanelStore.getState().taskFocusId).toBe("t2");
    expect(usePanelStore.getState().view).toBe("overview");
  });

  it("G2：产物点击 → 制品预览页签", async () => {
    render(<OverviewView scope={SESSION_SCOPE} />);
    fireEvent.click(await screen.findByTestId("panel-artifact-a1"));
    expect(usePanelStore.getState().activeTabId).toBe("artifact:a1");
  });

  it("SOP 优先：sop 非 null 显示流程节点分组（不显示 Todo）", async () => {
    vi.mocked(panelApi.sop).mockResolvedValueOnce({
      session_id: "s1",
      sop: {
        scope_id: "s1",
        revision: 1,
        active: {
          task_id: "task1",
          skill_id: "skill-x",
          node_id: "node-a",
          status: "running",
          awaiting_fields: ["amount"],
          collected_fields: [],
          updated_at: "",
        },
        pending: [],
        completed_count: 2,
      },
    });
    render(<OverviewView scope={SESSION_SCOPE} />);
    expect(await screen.findByTestId("panel-sop")).toBeInTheDocument();
    expect(screen.getByText("node-a")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-plan-todo")).not.toBeInTheDocument();
  });

  it("计划段显示团队工作项（project_id 过滤）；概览无团队板块", async () => {
    useChatStore.setState({ activeProjectId: "p1", tasksBySession: {} });
    vi.mocked(companyApi.projects).mockResolvedValueOnce({
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
          ],
        },
      ],
    });
    vi.mocked(companyApi.threads).mockResolvedValueOnce({
      threads: [
        {
          thread_id: "th-1",
          session_id: "s1",
          parent_task_id: null,
          participants: [],
          created_at: "",
          message_count: 0,
        },
      ],
    });
    vi.mocked(companyApi.workItems).mockResolvedValueOnce({
      items: [
        {
          id: "w1",
          title: "本团队卡",
          phase: "RUNNING",
          assignee_role: "dev",
          project_id: "p1",
          thread_id: "th-1",
        },
        {
          id: "w2",
          title: "他项目卡",
          phase: "RUNNING",
          assignee_role: "dev",
          project_id: "p2",
          thread_id: "th-1",
        },
      ],
      column_counts: {},
    });
    render(<OverviewView scope={SESSION_SCOPE} />);

    // 计划段承载团队工作项（project_id 优先过滤；他项目卡排除）
    expect(await screen.findByTestId("panel-work-items")).toBeInTheDocument();
    expect(screen.getByText("本团队卡")).toBeInTheDocument();
    expect(screen.queryByText("他项目卡")).not.toBeInTheDocument();
    // 团队板块已删：无 panel-team，且不再请求项目用量
    expect(screen.queryByTestId("panel-team")).not.toBeInTheDocument();
    expect(panelApi.projectUsage).not.toHaveBeenCalled();
    // 产物严格按会话作用域：只列本会话产出，不拉项目聚合历史文件
    expect(sessionsApi.artifacts).toHaveBeenCalledWith("s1");
    expect(panelApi.projectArtifacts).not.toHaveBeenCalled();
  });

  it("旧工作项无 project_id 时回落项目成员角色过滤", async () => {
    useChatStore.setState({ activeProjectId: "p1", tasksBySession: {} });
    vi.mocked(companyApi.projects).mockResolvedValueOnce({
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
          ],
        },
      ],
    });
    vi.mocked(companyApi.threads).mockResolvedValueOnce({
      threads: [
        {
          thread_id: "th-1",
          session_id: "s1",
          parent_task_id: null,
          participants: [],
          created_at: "",
          message_count: 0,
        },
      ],
    });
    vi.mocked(companyApi.workItems).mockResolvedValueOnce({
      items: [
        { id: "w1", title: "经久卡", phase: "RUNNING", assignee_role: "dev", thread_id: "th-1" },
        {
          id: "w2",
          title: "外角色卡",
          phase: "RUNNING",
          assignee_role: "other",
          thread_id: "th-1",
        },
      ],
      column_counts: {},
    });
    render(<OverviewView scope={SESSION_SCOPE} />);
    expect(await screen.findByTestId("panel-work-items")).toBeInTheDocument();
    expect(screen.getByText("经久卡")).toBeInTheDocument();
    expect(screen.queryByText("外角色卡")).not.toBeInTheDocument();
  });

  it("只展示当前会话线程的工作项——历史线程同名卡不进右栏", async () => {
    useChatStore.setState({ activeProjectId: "p1", tasksBySession: {} });
    vi.mocked(companyApi.projects).mockResolvedValueOnce({
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
          ],
        },
      ],
    });
    vi.mocked(companyApi.threads).mockResolvedValueOnce({
      threads: [
        {
          thread_id: "th-current",
          session_id: "s1",
          parent_task_id: null,
          participants: [],
          created_at: "",
          message_count: 0,
        },
      ],
    });
    vi.mocked(companyApi.workItems).mockResolvedValueOnce({
      items: [
        {
          id: "w1",
          title: "本会话卡",
          phase: "RUNNING",
          assignee_role: "dev",
          project_id: "p1",
          thread_id: "th-current",
        },
        {
          id: "w2",
          title: "历史会话卡",
          phase: "QUEUED",
          assignee_role: "dev",
          project_id: "p1",
          thread_id: "th-old",
        },
      ],
      column_counts: {},
    });
    render(<OverviewView scope={SESSION_SCOPE} />);
    expect(await screen.findByTestId("panel-work-items")).toBeInTheDocument();
    expect(screen.getByText("本会话卡")).toBeInTheDocument();
    expect(screen.queryByText("历史会话卡")).not.toBeInTheDocument();
  });

  it("员工作用域：直出单员工卡，token 走直聊线程而非项目累计", async () => {
    vi.mocked(companyApi.employees).mockResolvedValueOnce({
      employees: [{ employee_id: "e1", role_id: "dev", name: "小美", description: "开发" }],
    });
    vi.mocked(companyApi.threads).mockResolvedValueOnce({
      threads: [
        {
          thread_id: "th-dev",
          session_id: "dm-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 2,
          kind: "dm",
        },
      ],
    });
    render(<OverviewView scope={EMPLOYEE_SCOPE} />);
    expect(await screen.findByTestId("panel-employee-dev")).toBeInTheDocument();
    expect(screen.getByText("小美")).toBeInTheDocument();
    await waitFor(() => expect(panelApi.costDetail).toHaveBeenCalledWith("dm-dev"));
    expect(panelApi.sop).toHaveBeenCalledWith("dm-dev");
    // 项目累计 token 不得覆盖单聊会话 token
    expect(panelApi.projectUsage).not.toHaveBeenCalled();
    expect(vi.mocked(companyApi.workItems)).toHaveBeenCalledWith("dev");
    expect(panelApi.projectArtifacts).toHaveBeenCalledWith("p1", "dev");
    expect(panelApi.employeeProfile).toHaveBeenCalledWith("dev");
  });

  it("秘书会话：右栏计划不取团队工作项（计划=助理自己的 Todo，不混团队计划）", async () => {
    useChatStore.setState({
      activeProjectId: "p1",
      sessions: [
        {
          id: "s-sec",
          title: "助理会话",
          created_at: "",
          message_count: 0,
          project_id: "p1",
          origin: "secretary",
          status: "active",
        },
      ],
      tasksBySession: { "s-sec": tasks },
    });
    render(
      <OverviewView scope={{ kind: "session", key: "session:s-sec", sessionId: "s-sec" }} />,
    );
    // 会话 Todo 照常展示（助理自己的计划）；团队工作项不请求、不渲染
    await screen.findByTestId("panel-plan-todo");
    expect(vi.mocked(companyApi.workItems)).not.toHaveBeenCalled();
    expect(screen.queryByTestId("panel-work-items")).not.toBeInTheDocument();
  });
});

describe("OverviewView 计划段：SOP 全部完成后的降级", () => {
  const workItem = {
    id: "w1",
    title: "发布前检查",
    phase: "RUNNING",
    assignee_role: "dev",
    project_id: "p1",
    thread_id: "th-1",
  };

  const mockProjectAndThread = () => {
    vi.mocked(companyApi.projects).mockResolvedValueOnce({
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
          ],
        },
      ],
    } as never);
    vi.mocked(companyApi.threads).mockResolvedValueOnce({
      threads: [
        {
          thread_id: "th-1",
          session_id: "s1",
          parent_task_id: null,
          participants: [],
          created_at: "",
          message_count: 0,
        },
      ],
    });
  };

  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ activeProjectId: "", sessions: [], tasksBySession: {} });
    usePanelStore.setState({
      open: true,
      view: "overview",
      taskFocusId: "",
      tabs: [],
      activeTabId: "",
      fileFocusPath: "",
    });
  });

  it("SOP 全部完成 → 一行摘要 + 团队工作项照常可见（流程分组不再独占计划段）", async () => {
    useChatStore.setState({ activeProjectId: "p1", sessions: [], tasksBySession: {} });
    vi.mocked(panelApi.sop).mockResolvedValueOnce({
      session_id: "s1",
      sop: { scope_id: "s1", revision: 3, active: null, pending: [], completed_count: 3 },
    });
    mockProjectAndThread();
    vi.mocked(companyApi.workItems).mockResolvedValueOnce({
      items: [workItem],
      column_counts: {},
    });

    render(<OverviewView scope={SESSION_SCOPE} />);

    expect(await screen.findByTestId("panel-sop-summary")).toHaveTextContent("已完成: 3");
    expect(await screen.findByTestId("panel-work-items")).toBeInTheDocument();
    expect(screen.getByText("发布前检查")).toBeInTheDocument();
    // SOP 只以一行摘要出现，完整流程分组不再渲染
    expect(screen.queryByTestId("panel-sop")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("panel-sop-summary")).toHaveLength(1);
  });

  it("SOP 全部完成且无团队工作项 → 摘要与会话待办同时可见", async () => {
    useChatStore.setState({ activeProjectId: "", sessions: [], tasksBySession: { s1: tasks } });
    vi.mocked(panelApi.sop).mockResolvedValueOnce({
      session_id: "s1",
      sop: { scope_id: "s1", revision: 4, active: null, pending: [], completed_count: 2 },
    });

    render(<OverviewView scope={SESSION_SCOPE} />);

    expect(await screen.findByTestId("panel-sop-summary")).toHaveTextContent("已完成: 2");
    expect(screen.getByTestId("panel-plan-todo")).toBeInTheDocument();
    expect(screen.getByText("调研")).toBeInTheDocument();
    expect(screen.getByText("撰写")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-sop")).not.toBeInTheDocument();
  });

  it("SOP 仍有待执行节点 → 只显示流程分组，工作项与待办让位（SOP 不重复渲染）", async () => {
    useChatStore.setState({ activeProjectId: "p1", sessions: [], tasksBySession: { s1: tasks } });
    vi.mocked(panelApi.sop).mockResolvedValueOnce({
      session_id: "s1",
      sop: {
        scope_id: "s1",
        revision: 5,
        active: null,
        pending: [
          {
            task_id: "task2",
            skill_id: "skill-y",
            node_id: "node-b",
            status: "pending",
            awaiting_fields: [],
            collected_fields: [],
            updated_at: "",
          },
        ],
        completed_count: 1,
      },
    });
    mockProjectAndThread();
    let resolveItems: (value: Awaited<ReturnType<typeof companyApi.workItems>>) => void = () =>
      undefined;
    const deferred = new Promise<Awaited<ReturnType<typeof companyApi.workItems>>>((resolve) => {
      resolveItems = resolve;
    });
    vi.mocked(companyApi.workItems).mockReturnValue(deferred);

    render(<OverviewView scope={SESSION_SCOPE} />);

    expect(await screen.findByTestId("panel-sop")).toBeInTheDocument();
    expect(screen.getByText("node-b")).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(companyApi.workItems)).toHaveBeenCalled());

    await act(async () => {
      resolveItems({ items: [workItem], column_counts: {} });
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.queryByTestId("panel-work-items")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-plan-todo")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-sop-summary")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("panel-sop")).toHaveLength(1);
  });
});
