/**
 * 功能：顶栏项目面板联动测试——
 * 面板收起且在当前项目会话中：显示 [展开项目面板]；展开/普通会话：不显示。
 * .2：右栏开合入口对 DM 路由放宽（无 activeSessionId 也可展开）。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppHeader } from "@/components/layout/AppHeader";
import { SidebarProvider } from "@/components/ui/sidebar";
import { useChatStore } from "@/stores/chat-store";
import { useKnowledgeStore } from "@/stores/knowledge-store";
import { usePanelStore } from "@/stores/panel-store";

vi.mock("@/hooks/use-employees", () => ({
  useEmployees: () => ({
    employees: [{ role_id: "dev", employee_id: "e1", name: "小美" }],
    loaded: true,
  }),
}));

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      projects: vi.fn(async () => ({
        projects: [{ project_id: "p1", team_ids: ["aid"] }],
      })),
      teams: vi.fn(async () => ({
        teams: [{ template_id: "aid", team_id: "aid", name: "AI 设计团队" }],
      })),
    },
  };
});

function renderHeader(
  open: boolean,
  onTogglePanels = vi.fn(),
  route = "/",
  panelsCollapsed = false,
) {
  return render(
    <SidebarProvider open={open}>
      <MemoryRouter initialEntries={[route]}>
        <AppHeader onTogglePanels={onTogglePanels} panelsCollapsed={panelsCollapsed} />
      </MemoryRouter>
    </SidebarProvider>,
  );
}

describe("顶栏右栏开合（B1.2 放宽）", () => {
  beforeEach(() => {
    useChatStore.setState({ activeSessionId: null, activeProjectId: "" });
    usePanelStore.setState({ open: false });
  });

  it("DM 路由：无 activeSessionId 也显示右栏开关；点击展开", () => {
    renderHeader(true, vi.fn(), "/chat?view=dm&target=dev&project=p1");
    const trigger = screen.getByTestId("panel-toggle-header");
    fireEvent.click(trigger);
    expect(usePanelStore.getState().open).toBe(true);
  });

  it("DM 无 target：不显示右栏开关（无作用域）", () => {
    renderHeader(true, vi.fn(), "/chat?view=dm");
    expect(screen.queryByTestId("panel-toggle-header")).not.toBeInTheDocument();
  });

  it("破坏者：会话路由无会话不显示；面板已展开不显示（单一入口）", () => {
    renderHeader(true);
    expect(screen.queryByTestId("panel-toggle-header")).not.toBeInTheDocument();

    useChatStore.setState({ activeSessionId: "s1" });
    usePanelStore.setState({ open: true });
    renderHeader(true);
    expect(screen.queryByTestId("panel-toggle-header")).not.toBeInTheDocument();
  });
});

describe("AppHeader 项目面板联动（左栏常驻）", () => {
  beforeEach(() => {
    useChatStore.setState({
      activeSessionId: null,
      sessions: [],
      activeProjectId: "",
      pendingSessionOrigin: "",
    });
  });

  it("面板展开态：不显示展开按钮", () => {
    renderHeader(true, vi.fn(), "/", false);
    expect(screen.queryByTestId("project-panel-expand")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sidebar-expand")).not.toBeInTheDocument();
    expect(screen.queryByTestId("header-new-session")).not.toBeInTheDocument();
  });

  it("面板收起且项目会话：显示 [展开项目面板]，点击回调 onTogglePanels", () => {
    const onTogglePanels = vi.fn();
    useChatStore.setState({ activeProjectId: "p1" });
    renderHeader(true, onTogglePanels, "/?project=p1", true);
    const expand = screen.getByTestId("project-panel-expand");
    fireEvent.click(expand);
    expect(onTogglePanels).toHaveBeenCalledTimes(1);
  });

  it("破坏者：普通会话（无项目归属）即便收起也不显示展开按钮", () => {
    useChatStore.setState({ activeProjectId: "" });
    renderHeader(true, vi.fn(), "/", true);
    expect(screen.queryByTestId("project-panel-expand")).not.toBeInTheDocument();
  });
});

describe("AppHeader 单聊标题（团队名口径）", () => {
  beforeEach(() => {
    useChatStore.setState({
      activeSessionId: "s-group",
      sessions: [
        {
          id: "s-group",
          title: "项目总群聊",
          created_at: "",
          message_count: 2,
          project_id: "p1",
        } as never,
      ],
      activeProjectId: "p1",
    });
  });

  it("非 DM 会话仍显示会话标题与重命名入口（回归）", () => {
    renderHeader(true, vi.fn(), "/?project=p1");
    expect(screen.getByText("项目总群聊")).toBeInTheDocument();
    expect(screen.getByTestId("header-session-rename")).toBeInTheDocument();
  });
});

describe("AppHeader 知识图谱路由（图谱页顶栏化）", () => {
  beforeEach(() => {
    useChatStore.setState({
      activeSessionId: null,
      sessions: [],
      activeProjectId: "",
      pendingSessionOrigin: "",
    });
    useKnowledgeStore.getState().resetQuery();
  });

  it("知识页顶栏不渲染（标题与搜索由页面内承载）", () => {
    const { container } = renderHeader(true, vi.fn(), "/knowledge");
    expect(container.querySelector("header")).toBeNull();
    expect(screen.queryByRole("searchbox", { name: "搜索实体" })).not.toBeInTheDocument();
  });

  it("工作页与团队页同一外壳，不渲染全局顶栏", () => {
    const { container } = renderHeader(true, vi.fn(), "/work");
    expect(container.querySelector("header")).toBeNull();
    expect(screen.queryByText("工作")).not.toBeInTheDocument();
  });

  it("团队路由不显示重复的顶栏标题", () => {
    renderHeader(true, vi.fn(), "/team");
    expect(screen.queryByText("团队")).not.toBeInTheDocument();
  });

  it("技能页顶栏不渲染（标题由页面内承载）", () => {
    const { container } = renderHeader(true, vi.fn(), "/skills");
    expect(container.querySelector("header")).toBeNull();
    expect(screen.queryByText("技能·连接器·知识库")).not.toBeInTheDocument();
  });

  it("知识页顶栏不再写 knowledge-store（搜索由页面承载）", () => {
    useKnowledgeStore.getState().resetQuery();
    renderHeader(true, vi.fn(), "/knowledge");
    expect(useKnowledgeStore.getState().query).toBe("");
  });
});
it("DM 路由标题取员工名（不显示项目总群聊），且隐藏会话重命名", async () => {
  renderHeader(true, vi.fn(), "/chat?view=dm&target=dev&project=p1");
  expect(await screen.findByText("小美")).toBeInTheDocument();
  expect(screen.queryByText("项目总群聊")).not.toBeInTheDocument();
  expect(screen.queryByTestId("header-session-rename")).not.toBeInTheDocument();
});
