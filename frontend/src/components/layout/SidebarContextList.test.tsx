/**
 * 功能：SidebarContextList 行为测试（第五轮：折叠箭头 / 超 5 条「查看更多」/
 * 项目行菜单两项 / 项目内新建会话）。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarContextList } from "@/components/layout/SidebarContextList";
import { SidebarProvider } from "@/components/ui/sidebar";
import { sessionsApi } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

const { deleteProject, openFolder, renameProject, sixSessions, sixProjects } = vi.hoisted(() => ({
  deleteProject: vi.fn(async () => ({ deleted: true })),
  openFolder: vi.fn(async () => ({ opened: "C:/work" })),
  renameProject: vi.fn(async () => ({
    project: {
      project_id: "p1",
      name: "改名后的项目",
      root_dir: "",
      team_ids: [],
      created_at: "",
      updated_at: "",
      members: [],
    },
  })),
  sixSessions: Array.from({ length: 6 }, (_, index) => ({
    id: `s${index + 1}`,
    title: `会话${index + 1}`,
    created_at: "",
    message_count: 0,
    project_id: "",
  })),
  sixProjects: Array.from({ length: 6 }, (_, index) => ({
    project_id: `p${index + 1}`,
    name: `项目${index + 1}`,
    root_dir: "",
    team_ids: [] as string[],
    created_at: "",
    updated_at: "",
    members: [] as unknown[],
  })),
}));

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      projects: vi.fn(async () => ({ projects: sixProjects })),
      deleteProject,
      openFolder,
      renameProject,
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      list: vi.fn(async () => sixSessions),
      remove: vi.fn(async () => ({})),
    },
  };
});

function renderList(initialEntry = "/") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SidebarProvider>
        <SidebarContextList />
      </SidebarProvider>
    </MemoryRouter>,
  );
}

describe("SidebarContextList", () => {
  beforeEach(() => {
    deleteProject.mockClear();
    openFolder.mockClear();
    renameProject.mockClear();
    renameProject.mockResolvedValue({
      project: {
        project_id: "p1",
        name: "改名后的项目",
        root_dir: "",
        team_ids: [],
        created_at: "",
        updated_at: "",
        members: [],
      },
    });
    // 每个用例恢复默认列表（个别用例的 mockResolvedValue 覆盖需清除——防跨用例污染）
    vi.mocked(sessionsApi.list).mockResolvedValue(sixSessions);
    useChatStore.setState({
      sessions: sixSessions.map((item) => ({ ...item })),
      sessionsLoaded: true,
      pinnedSessionIds: [],
      activeSessionId: null,
      activeProjectId: "",
      dismissedProjectSessions: {},
    });
    useUiStore.setState({
      sidebarTasksOpen: true,
      sidebarProjectsOpen: true,
      sidebarTasksAll: false,
      sidebarProjectsAll: false,
      bulkSelectionActive: false,
    });
  });

  it("URL 项目参数同步到 store（直达项目上下文时 @派单归属不丢失）", async () => {
    renderList("/?project=p1");
    await waitFor(() => expect(useChatStore.getState().activeProjectId).toBe("p1"));
  });

  it("任务区不显示秘书入口行；秘书会话（无项目归属）作为普通会话列出并可打开", async () => {
    vi.mocked(sessionsApi.list).mockResolvedValue([
      {
        id: "sec-1",
        title: "秘书会话一",
        created_at: "2026-09-29T10:00:00",
        message_count: 1,
        project_id: "",
        origin: "secretary",
      },
      {
        id: "task-1",
        title: "普通任务一",
        created_at: "2026-09-29T09:00:00",
        message_count: 1,
        project_id: "",
      },
    ]);
    renderList();
    // 任务区不做秘书入口行（秘书单聊会话本身照常在任务区）
    expect(screen.queryByTestId("sidebar-secretary-entry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sidebar-secretary-new")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByText("秘书会话一"));
    expect(useChatStore.getState().activeSessionId).toBe("sec-1");
    expect(screen.getByText("普通任务一")).toBeInTheDocument();
  });

  it("点项目行默认打开该项目最新会话记录", async () => {
    const projectSessions = [
      {
        id: "s-old",
        title: "旧会话",
        created_at: "2026-09-20T10:00:00",
        message_count: 1,
        project_id: "p1",
      },
      {
        id: "s-new",
        title: "新会话",
        created_at: "2026-09-23T10:00:00",
        message_count: 1,
        project_id: "p1",
      },
    ];
    // 列表接口返回项目会话（组件挂载会拉取并覆盖 store）
    vi.mocked(sessionsApi.list).mockResolvedValue(projectSessions);
    renderList();

    fireEvent.click(await screen.findByText("项目1"));

    expect(useChatStore.getState().activeSessionId).toBe("s-new");
    expect(useChatStore.getState().activeProjectId).toBe("p1");
    // 恢复默认列表（后续用例依赖 sixSessions）
    vi.mocked(sessionsApi.list).mockResolvedValue(sixSessions);
  });

  it("项目无任何会话时点项目行：进入项目会话新建态（B1.3 删甲——不再跳群聊视图）", async () => {
    // 破坏者视角：store 里残留他会话时也必须清空——否则 ChatView 会显示旧会话而非新会话态
    useChatStore.setState({ activeSessionId: "leftover-session" });
    renderList("/?project=p1");

    fireEvent.click(await screen.findByText("项目1"));

    await waitFor(() => {
      expect(useChatStore.getState().activeSessionId).toBeNull();
    });
    expect(useChatStore.getState().activeProjectId).toBe("p1");
  });

  it("任务区默认显示 5 条，超出显示「查看更多（N）」并可收起", async () => {
    renderList();
    await screen.findByText("会话1");
    expect(screen.queryByText("会话6")).not.toBeInTheDocument();
    const more = screen.getByTestId("tasks-more");
    expect(more.textContent).toBe("查看更多（1）");
    fireEvent.click(more);
    expect(screen.getByText("会话6")).toBeInTheDocument();
    expect(screen.getByTestId("tasks-more").textContent).toBe("收起");
    fireEvent.click(screen.getByTestId("tasks-more"));
    expect(screen.queryByText("会话6")).not.toBeInTheDocument();
  });

  it("点区标题箭头可折叠/展开任务列表", async () => {
    renderList();
    await screen.findByText("会话1");
    fireEvent.click(screen.getByTestId("section-tasks"));
    expect(screen.queryByText("会话1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("section-tasks"));
    expect(screen.getByText("会话1")).toBeInTheDocument();
  });

  it("项目区超 5 条「查看更多（N）」可展开（项目行新建会话按钮已移除）", async () => {
    renderList();
    await screen.findByText("项目1");
    expect(screen.queryByText("项目6")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("projects-more"));
    expect(screen.getByText("项目6")).toBeInTheDocument();
    expect(screen.queryByTitle("新建会话")).not.toBeInTheDocument();
  });

  it("项目菜单「从列表中删除」确认后调用删除并从列表移除", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("项目1");
    // 更多按钮平时隐形（hover 才浮现）——先悬浮项目行再点
    fireEvent.mouseEnter(
      screen.getAllByTestId("project-menu-trigger")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("project-menu-trigger")[0]);
    await user.click(await screen.findByTestId("project-menu-remove"));
    await user.click(await screen.findByTestId("project-remove-confirm"));
    await waitFor(() => {
      expect(deleteProject).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(screen.queryByText("项目1")).not.toBeInTheDocument();
    });
  });

  it("删除项目后其旧会话不回流任务区（解绑语义；磁盘与后端记录不动）", async () => {
    const projectSessions = [
      {
        id: "ps-old",
        title: "项目旧会话",
        created_at: "2026-09-20T10:00:00",
        message_count: 1,
        project_id: "p1",
      },
      {
        id: "task-keep",
        title: "普通任务会话",
        created_at: "2026-09-20T09:00:00",
        message_count: 1,
        project_id: "",
      },
    ];
    vi.mocked(sessionsApi.list).mockResolvedValue(projectSessions);
    const user = userEvent.setup();
    renderList();
    await screen.findByText("项目1");

    // 删除前：项目会话归项目，不在任务区
    expect(screen.queryByText("项目旧会话")).not.toBeInTheDocument();

    fireEvent.mouseEnter(
      screen.getAllByTestId("project-menu-trigger")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("project-menu-trigger")[0]);
    await user.click(await screen.findByTestId("project-menu-remove"));
    await user.click(await screen.findByTestId("project-remove-confirm"));

    // 删除后：项目行消失，且旧会话既不在任务区也不在任何位置显示
    await waitFor(() => expect(screen.queryByText("项目1")).not.toBeInTheDocument());
    expect(screen.queryByText("项目旧会话")).not.toBeInTheDocument();
    // 无项目归属的普通会话不受影响
    expect(screen.getByText("普通任务会话")).toBeInTheDocument();
    expect(useChatStore.getState().dismissedProjectSessions.p1).toEqual(["ps-old"]);
    vi.mocked(sessionsApi.list).mockResolvedValue(sixSessions);
  });

  it("重新加入该项目后新会话正常显示（隐藏位只含删除前的旧会话）", async () => {
    useChatStore.setState({ dismissedProjectSessions: { p1: ["ps-old"] } });
    vi.mocked(sessionsApi.list).mockResolvedValue([
      {
        id: "ps-old",
        title: "项目旧会话",
        created_at: "2026-09-20T10:00:00",
        message_count: 1,
        project_id: "p1",
      },
      {
        id: "ps-new",
        title: "重新加入后的新会话",
        created_at: "2026-10-01T10:00:00",
        message_count: 1,
        project_id: "p1",
      },
    ]);
    renderList();
    await screen.findByText("项目1");

    // 点项目行进该项目最新会话（新会话，不在隐藏位内）
    fireEvent.click(screen.getByText("项目1"));
    await waitFor(() => expect(useChatStore.getState().activeSessionId).toBe("ps-new"));
    expect(screen.queryByText("项目旧会话")).not.toBeInTheDocument();
    vi.mocked(sessionsApi.list).mockResolvedValue(sixSessions);
  });

  it("项目菜单「重命名」：弹窗改名成功后就地更新列表项（不重拉、不新建项目）", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("项目1");
    const rowsBefore = screen.getAllByTestId("project-menu-trigger").length;

    // 更多按钮平时隐形（hover 才浮现）——先悬浮项目行再点
    fireEvent.mouseEnter(
      screen.getAllByTestId("project-menu-trigger")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("project-menu-trigger")[0]);
    await user.click(await screen.findByTestId("project-menu-rename"));

    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("项目1");
    await user.clear(input);
    await user.type(input, "  改名后的项目  ");
    await user.click(screen.getByRole("button", { name: "确定" }));

    await waitFor(() => expect(renameProject).toHaveBeenCalledWith("p1", "改名后的项目"));
    await waitFor(() => expect(screen.getByText("改名后的项目")).toBeInTheDocument());
    expect(screen.queryByText("项目1")).not.toBeInTheDocument();
    // 仅就地替换：项目行数不变，未误增条目
    expect(screen.getByText("项目2")).toBeInTheDocument();
    expect(screen.getAllByTestId("project-menu-trigger")).toHaveLength(rowsBefore);
  });

  it("项目改名失败：保留项目行与弹窗（不乐观清空，可重试）", async () => {
    const user = userEvent.setup();
    renameProject.mockRejectedValueOnce(new Error("boom"));
    renderList();
    await screen.findByText("项目1");

    // 更多按钮平时隐形（hover 才浮现）——先悬浮项目行再点
    fireEvent.mouseEnter(
      screen.getAllByTestId("project-menu-trigger")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("project-menu-trigger")[0]);
    await user.click(await screen.findByTestId("project-menu-rename"));
    await user.clear(screen.getByRole("textbox"));
    await user.type(screen.getByRole("textbox"), "改名失败也保留");
    await user.click(screen.getByRole("button", { name: "确定" }));

    await waitFor(() => expect(renameProject).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("textbox")).toBeInTheDocument());
    expect(screen.getByText("项目1")).toBeInTheDocument();
    expect(screen.queryByText("改名失败也保留")).not.toBeInTheDocument();
  });

  it("批量模式：面板内分区（预选当前项 / 取消后禁用 / 全选后启用；非弹窗）", async () => {
    const user = userEvent.setup();
    renderList();
    await screen.findByText("会话1");    fireEvent.mouseEnter(screen.getAllByTitle("更多操作")[0].parentElement as HTMLElement);

    await user.click(screen.getAllByTitle("更多操作")[0]);
    await user.click(await screen.findByText("批量选择"));
    const panel = screen.getByTestId("bulk-panel");
    expect(panel).toBeInTheDocument();
    // 分区式（贴合面板两边），不是悬浮弹窗
    expect(panel.className).toContain("border-t");
    expect(panel.className).not.toContain("fixed");
    // 进入批量预选当前会话 → 按钮可用
    expect(screen.getByText(/已选 1 项/)).toBeInTheDocument();
    expect(screen.getByTestId("bulk-delete-btn")).toBeEnabled();
    // 取消该选择 → 按钮禁用（浅色态）
    await user.click(screen.getByText("会话1"));
    expect(screen.getByTestId("bulk-delete-btn")).toBeDisabled();
    expect(screen.getByTestId("bulk-archive-btn")).toBeDisabled();
    // 全选 → 启用 + 计数
    await user.click(screen.getByTestId("bulk-select-all"));
    expect(screen.getByTestId("bulk-delete-btn")).toBeEnabled();
    expect(screen.getByTestId("bulk-archive-btn")).toBeEnabled();
    expect(screen.getByText(/已选 6 项/)).toBeInTheDocument();
    // 批量激活同步 flag（AppSidebar 隐藏底部按钮 + 分区贴底）
    expect(useUiStore.getState().bulkSelectionActive).toBe(true);
    await user.click(screen.getByTitle("取消"));
    expect(useUiStore.getState().bulkSelectionActive).toBe(false);
  });

  it("批量删除中途失败：已删项剔除、未删项保留供重试（137b-3 修复）", async () => {
    const user = userEvent.setup();
    const remove = vi.mocked(sessionsApi.remove);
    remove.mockReset();
    remove.mockResolvedValueOnce({} as never);
    remove.mockRejectedValueOnce(new Error("boom"));
    renderList();
    await screen.findByText("会话1");    fireEvent.mouseEnter(screen.getAllByTitle("更多操作")[0].parentElement as HTMLElement);

    await user.click(screen.getAllByTitle("更多操作")[0]);
    await user.click(await screen.findByText("批量选择"));
    await user.click(screen.getByTestId("bulk-select-all"));
    expect(screen.getByText(/已选 6 项/)).toBeInTheDocument();
    await user.click(screen.getByTestId("bulk-delete-btn"));
    await user.click(await screen.findByTestId("bulk-delete-confirm"));

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    // 第一个成功（剔除）→ 剩 5 项选中（未删项保留，可重试）——原实现残留已删 id 致计数虚高
    await waitFor(() => {
      expect(screen.getByText(/已选 5 项/)).toBeInTheDocument();
    });
  });

  it("收缩左栏再展开（卸载重挂）后，任务段折叠态保持——不自动打开", async () => {
    const view = renderList();
    await screen.findByText("会话1");
    fireEvent.click(screen.getByTestId("section-tasks"));
    expect(screen.queryByText("会话1")).not.toBeInTheDocument();

    // 左栏 offcanvas 折叠会卸载列表区，再展开时重新挂载（AppSidebar showContextList）
    view.unmount();
    renderList();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.queryByText("会话1")).not.toBeInTheDocument();
  });

  it("收缩左栏再展开（卸载重挂）后，项目空间段折叠态保持——不自动打开", async () => {
    const view = renderList();
    await screen.findByText("项目1");
    fireEvent.click(screen.getByTestId("section-projects"));
    expect(screen.queryByText("项目1")).not.toBeInTheDocument();

    view.unmount();
    renderList();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(screen.queryByText("项目1")).not.toBeInTheDocument();
  });

  it("收缩左栏再展开后，「查看更多」展开态保持（同族：卸载不重置）", async () => {
    const view = renderList();
    await screen.findByText("会话1");
    fireEvent.click(screen.getByTestId("tasks-more"));
    expect(screen.getByText("会话6")).toBeInTheDocument();

    view.unmount();
    renderList();
    expect(screen.getByText("会话6")).toBeInTheDocument();
  });

  it("段落折叠偏好持久化到 bandi:ui（刷新后恢复）", async () => {
    renderList();
    await screen.findByText("会话1");
    fireEvent.click(screen.getByTestId("section-tasks"));
    const persisted = JSON.parse(localStorage.getItem("bandi:ui") ?? "{}") as {
      state?: { sidebarTasksOpen?: boolean };
    };
    expect(persisted.state?.sidebarTasksOpen).toBe(false);
  });

  // 项目行也有任务状态（聚合组内会话）；「更多」改悬浮显示
  describe("项目行状态与悬浮更多", () => {
    const projectSession = (id: string) => ({
      id,
      title: id,
      created_at: "",
      message_count: 0,
      project_id: "p1",
    });

    it("组内有会话在跑 → 项目行显示执行中转圈", async () => {
      renderList();
      await screen.findByText("会话1");
      act(() => {
        useChatStore.setState({
          sessions: [projectSession("ps1")],
          sessionActivity: { ps1: "running" },
        });
      });
      expect(await screen.findByTestId("project-activity-p1-running")).toBeInTheDocument();
      expect(screen.queryByTestId("project-activity-p1-done")).not.toBeInTheDocument();
    });

    it("组内会话已完成 → 显示完成圆点（在跑优先的对照面）", async () => {
      renderList();
      await screen.findByText("会话1");
      act(() => {
        useChatStore.setState({
          sessions: [projectSession("ps1")],
          sessionActivity: { ps1: "done" },
        });
      });
      expect(await screen.findByTestId("project-activity-p1-done")).toBeInTheDocument();
    });

    it("组内无活动 → 不渲染任何状态（不占位）", async () => {
      renderList();
      await screen.findByText("项目1");
      act(() => {
        useChatStore.setState({ sessions: [projectSession("ps1")], sessionActivity: {} });
      });
      expect(screen.queryByTestId("project-activity-p1-running")).not.toBeInTheDocument();
      expect(screen.queryByTestId("project-activity-p1-done")).not.toBeInTheDocument();
    });

    it("项目「更多」平时隐形（pointer-events: none），悬浮行后浮现可点", async () => {
      renderList();
      await screen.findByText("项目1");
      const trigger = screen.getAllByTestId("project-menu-trigger")[0];
      expect(trigger).toHaveStyle({ opacity: "0", pointerEvents: "none" });

      fireEvent.mouseEnter(trigger.parentElement as HTMLElement);
      await waitFor(() => expect(trigger).toHaveStyle({ opacity: "1" }));
      expect(trigger).toHaveStyle({ pointerEvents: "auto" });
    });

    it("打开条目即已读：done 圆点被清除（running 不清）", async () => {
      renderList();
      await screen.findByText("会话1");
      act(() => {
        useChatStore.setState({ sessionActivity: { s1: "done" } });
      });
      fireEvent.click(screen.getByText("会话1"));
      await waitFor(() => {
        expect(useChatStore.getState().sessionActivity.s1).toBeUndefined();
      });
    });
  });
});
