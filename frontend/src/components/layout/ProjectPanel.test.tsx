/**
 * 功能：ProjectPanel 行为测试（第六轮：团队群聊会话同步 / 收缩态不渲染）。
 * 第十四轮：三类头像行「＋新建会话」（聊天气泡加号）+「更多操作 ⋯」
 * 上部搜索 icon 点击展开为搜索框、未输入时呈现历史任务（含归属分区字段、覆盖员工会话）
 * 会话行不显示条数、员工行数据源 = 服务端直聊线程 ∪ 本地 store。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectPanel } from "@/components/layout/ProjectPanel";
import { ApiError } from "@/services/api";
import { companyApi } from "@/services/company";
import { sessionsApi, type SessionSummary } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

function projectRecord(members: boolean) {
  return {
    project_id: "p1",
    name: "选品项目",
    root_dir: "",
    team_ids: members ? ["t1"] : [],
    created_at: "",
    updated_at: "",
    members: members
      ? [
          {
            team_id: "t1",
            role_id: "r1",
            role_name: "运营",
            employee_id: "e1",
            employee_name: "小美",
            employee_status: "active",
          },
        ]
      : [],
  };
}

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      projects: vi.fn(async () => ({ projects: [projectRecord(true)] })),
      employees: vi.fn(async () => ({ employees: [] })),
      inbox: vi.fn(async () => []),
      threads: vi.fn(async () => ({ threads: [] })),
      thread: vi.fn(async () => ({
        thread_id: "",
        parent_task_id: null,
        participants: [],
        created_at: "",
        messages: [],
      })),
      teams: vi.fn(async () => ({ teams: [] })),
      orgTeams: vi.fn(async () => ({
        teams: [{ team_id: "t1", name: "选品组", roles: [{ name: "运营" }] }],
      })),
      addTeam: vi.fn(async () => ({})),
      deleteProject: vi.fn(async () => ({})),
      deleteThread: vi.fn(async () => ({ deleted: true, thread_id: "" })),
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      remove: vi.fn(async () => ({})),
      create: vi.fn(async () => ({
        session: { id: "new-group-1", title: "项目总群聊", created_at: "2026-10-16T00:00:00" },
      })),
      archive: vi.fn(async () => ({
        summarized: true,
        distilled: true,
        archived: true,
        reason: "",
      })),
      list: vi.fn(async () => [] as unknown as SessionSummary[]),
    },
  };
});

function session(id: string, title: string, projectId: string, createdAt = "") {
  return { id, title, created_at: createdAt, message_count: 7, project_id: projectId };
}

/** 功能：面板会话测试数据（秘书 1 条 + 群聊 2 条，均属项目 p1）。 */
function panelSessions() {
  return [
    { ...session("sec-1", "秘书会话一", "p1", "2026-09-21T10:00:00"), origin: "secretary" },
    session("group-1", "团队会话一", "p1", "2026-09-20T10:00:00"),
    session("group-2", "团队会话二", "p1", "2026-09-19T10:00:00"),
  ] as never;
}

function renderPanel(collapsed = false) {
  return render(
    <MemoryRouter initialEntries={["/?project=p1"]}>
      <ProjectPanel collapsed={collapsed} />
    </MemoryRouter>,
  );
}

/** 带项目切换入口的渲染（URL ?project= 变化 → 切项目；projectId 来源即 URL 参数）。 */
function renderPanelWithNav() {
  function Harness() {
    const navigate = useNavigate();
    return (
      <>
        <button data-testid="goto-p2" onClick={() => navigate("/?project=p2")}>
          go p2
        </button>
        <ProjectPanel collapsed={false} />
      </>
    );
  }
  return render(
    <MemoryRouter initialEntries={["/?project=p1"]}>
      <Harness />
    </MemoryRouter>,
  );
}

describe("ProjectPanel", () => {
  beforeEach(() => {
    vi.mocked(companyApi.threads).mockResolvedValue({ threads: [] });
    vi.mocked(companyApi.projects).mockResolvedValue({ projects: [projectRecord(true)] });
    vi.mocked(companyApi.employees).mockResolvedValue({ employees: [] });
    useChatStore.setState({
      sessions: [
        session("s1", "团队会话一", "p1", "2026-09-20T10:00:00"),
        session("s2", "团队会话二", "p1", "2026-09-19T10:00:00"),
        session("s3", "无项目会话", ""),
      ],
      sessionsLoaded: true,
      activeSessionId: null,
    });
    // 助理条目：默认已激活（历史用例都假设可见）；「不常驻」用例单独清空验证
    useUiStore.setState({ projectPanelWidthOverride: null, assistantEntryProjects: ["p1"] });
  });

  it("离开会话路由后不渲染项目面板", () => {
    render(
      <MemoryRouter initialEntries={["/work?project=p1"]}>
        <ProjectPanel collapsed={false} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId("project-panel")).not.toBeInTheDocument();
  });

  it("助理条目不再常驻：未选择项目 / 无秘书会话时不显示（2026-10-19 拍板）", async () => {
    useUiStore.setState({ assistantEntryProjects: [] });
    renderPanel();
    await screen.findByTestId("project-panel");
    expect(screen.queryByTestId("panel-secretary-entry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-secretary-new-session")).not.toBeInTheDocument();
  });

  it("项目面板宽度：手动覆盖生效；双击清除覆盖回窗口自适应（2026-10-04；上限收到 224 于 2026-10-05）", async () => {
    useUiStore.setState({ projectPanelWidthOverride: 220 });
    renderPanel();
    const panel = await screen.findByTestId("project-panel");
    expect(panel.style.width).toBe("220px");
    const handle = screen.getByTestId("project-panel-resize-handle");
    expect(handle).toBeInTheDocument();
    fireEvent.doubleClick(handle);
    expect(useUiStore.getState().projectPanelWidthOverride).toBeNull();
    // jsdom 窗口宽 1024 = 自适应下限（1024→160）
    expect(panel.style.width).toBe("160px");
  });

  it("破坏者：手动覆盖有最小/最大保护（160 ~ 224，上限 = 自适应最大宽度）", () => {
    useUiStore.getState().setProjectPanelWidth(100);
    expect(useUiStore.getState().projectPanelWidthOverride).toBe(160);
    useUiStore.getState().setProjectPanelWidth(999);
    expect(useUiStore.getState().projectPanelWidthOverride).toBe(224);
  });

  it("破坏者：旧持久化 override 超上限时读时钳制（不经 setProjectPanelWidth）", async () => {
    // 上限收紧前用户曾拖到 320 并落盘——重新打开页面不得按 320 渲染
    useUiStore.setState({ projectPanelWidthOverride: 320 });
    renderPanel();
    const panel = await screen.findByTestId("project-panel");
    expect(panel.style.width).toBe("224px");
  });

  it("项目会话两类分列：团队群聊条目化（无会话列表）/ 秘书会话（origin 标记）+ 无项目归任务列表", async () => {
    useChatStore.setState({
      sessions: [
        session("s1", "团队会话一", "p1"),
        session("s2", "团队会话二", "p1"),
        { ...session("s3", "秘书会话一", "p1"), origin: "secretary" },
        session("s4", "无项目会话", ""),
      ],
    });
    renderPanel();

    // 团队群聊：单条目（条目即入口）——历史群聊会话不再成列
    expect(await screen.findByTestId("panel-group-entry")).toBeInTheDocument();
    expect(screen.queryAllByTestId("panel-project-session")).toHaveLength(0);
    expect(screen.queryByText("团队会话一")).not.toBeInTheDocument();
    expect(screen.queryByText("团队会话二")).not.toBeInTheDocument();

    const secretaryRows = await screen.findAllByTestId("panel-secretary-session");
    expect(secretaryRows).toHaveLength(1);
    expect(secretaryRows[0]).toHaveTextContent("秘书会话一");
    expect(screen.getByTestId("panel-secretary-entry")).toHaveTextContent("助");

    expect(screen.queryByText("无项目会话")).not.toBeInTheDocument();
  });

  it("已归档会话（status=archived）不显示在面板中", async () => {
    // 功能：归档隐藏以服务端 status 为准，前端按 status 兜底过滤
    useChatStore.setState({
      sessions: [
        {
          id: "s1",
          title: "已归档秘书",
          created_at: "",
          message_count: 0,
          project_id: "p1",
          origin: "secretary",
          status: "archived",
        },
        {
          id: "s2",
          title: "在列秘书",
          created_at: "",
          message_count: 1,
          project_id: "p1",
          origin: "secretary",
          status: "active",
        },
      ] as never,
    });
    renderPanel();
    const rows = await screen.findAllByTestId("panel-secretary-session");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent("在列秘书");
    expect(screen.queryByText("已归档秘书")).not.toBeInTheDocument();
  });

  it("collapsed=true 时面板整体不渲染", () => {
    renderPanel(true);
    expect(screen.queryByTestId("project-panel")).not.toBeInTheDocument();
  });

  it("会话列表行只显示标题，不再显示消息条数", async () => {
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();
    const rows = await screen.findAllByTestId("panel-secretary-session");
    expect(rows[0].textContent).toBe("秘书会话一");
  });

  it("标题栏保留「功能」icon；秘书行内置「＋新建会话」+「更多操作」；群聊条目=打开+删除", async () => {
    renderPanel();

    expect(await screen.findByTestId("panel-feature-menu")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-history")).not.toBeInTheDocument();
    // 秘书行保留「＋新建会话」+「更多操作」
    expect(await screen.findByTestId("panel-secretary-new-session")).toBeInTheDocument();
    expect(screen.getByTestId("panel-secretary-more")).toBeInTheDocument();
    // 群聊条目化：条目即入口（打开 + 直接删除），不再有新建会话/更多菜单
    expect(await screen.findByTestId("panel-group-entry")).toBeInTheDocument();
    expect(screen.getByTestId("panel-group-open")).toBeInTheDocument();
    expect(screen.getByTestId("panel-group-delete")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-group-new-session")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-group-more")).not.toBeInTheDocument();
    expect(screen.queryByText("员工会话")).not.toBeInTheDocument();
  });

  it("秘书行「＋新建会话」平时隐形，悬浮该行才浮现（2026-10-19 拍板）", async () => {
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();
    const button = await screen.findByTestId("panel-secretary-new-session");
    expect(button.style.opacity).toBe("0");
    expect(button.style.pointerEvents).toBe("none");
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    expect(button.style.opacity).toBe("1");
    expect(button.style.pointerEvents).toBe("auto");
  });

  it("群聊删除按钮悬浮显示（2026-10-19 拍板）", async () => {
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();
    const groupDelete = await screen.findByTestId("panel-group-delete");
    expect(groupDelete.style.opacity).toBe("0");
    fireEvent.mouseEnter(screen.getByTestId("panel-group-entry"));
    expect(groupDelete.style.opacity).toBe("1");
  });

  it("面板头部收缩按钮：与选择团队同排，点击回调 onToggle", async () => {
    const onToggle = vi.fn();
    render(
      <MemoryRouter initialEntries={["/?project=p1"]}>
        <ProjectPanel collapsed={false} onToggle={onToggle} />
      </MemoryRouter>,
    );
    await screen.findByTestId("project-panel");
    const collapse = screen.getByTestId("project-panel-collapse");
    expect(collapse).toBeInTheDocument();
    const feature = screen.getByTestId("panel-feature-menu");
    expect(
      collapse.compareDocumentPosition(feature) & Node.DOCUMENT_POSITION_PRECEDING,
    ).toBeTruthy();
    fireEvent.click(collapse);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("项目内秘书会话隐藏功能菜单", async () => {
    useChatStore.setState({
      activeSessionId: "s-secretary",
      sessions: [
        {
          id: "s-secretary",
          title: "秘书会话",
          created_at: "",
          message_count: 1,
          project_id: "p1",
          origin: "secretary",
        },
      ] as never,
    });
    renderPanel();
    await screen.findByTestId("project-panel");
    expect(screen.queryByTestId("panel-feature-menu")).not.toBeInTheDocument();
  });

  it("秘书头像行 ⋯ = 分组级菜单（打开文件夹 + 批量选择），不含会话级项与项目删除", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();

    // 头像行「更多」平时隐形（hover 才浮现）——先悬浮该行再点
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    await user.click(await screen.findByTestId("panel-secretary-more"));
    const secretaryItems = (await screen.findAllByRole("menuitem")).map((el) =>
      el.textContent?.trim(),
    );
    expect(secretaryItems).toEqual(["打开文件夹", "批量选择"]);
    // 灰色不可用的会话级项不渲染（头像行不对应具体会话）
    expect(screen.queryByTestId("panel-session-pin")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-session-rename")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-session-delete")).not.toBeInTheDocument();
    // 项目级删除不在面板任何菜单里（入口在左栏项目行 ⋯）
    expect(screen.queryByText("从列表中删除")).not.toBeInTheDocument();
    expect(screen.queryByText("从项目列表中删除")).not.toBeInTheDocument();
  });

  it("头像行无会话时菜单仍只有可用的分组级项（不出现灰色会话项）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ sessions: [] });
    renderPanel();
    // 头像行「更多」平时隐形（hover 才浮现）——先悬浮该行再点
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    await user.click(await screen.findByTestId("panel-secretary-more"));
    const items = (await screen.findAllByRole("menuitem")).map((el) => el.textContent?.trim());
    expect(items).toEqual(["打开文件夹", "批量选择"]);
    expect(screen.queryByTestId("panel-session-delete")).not.toBeInTheDocument();
  });

  it("顶栏不提供项目级 ⋯ 菜单（项目删除只在左栏项目行）", async () => {
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();
    expect(await screen.findByTestId("panel-feature-menu")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-project-more")).not.toBeInTheDocument();
  });

  it("无团队项目：群聊历史不成列，但存在群聊会话时入口保留；秘书会话列表仍可见", async () => {
    vi.mocked(companyApi.projects).mockResolvedValue({ projects: [projectRecord(false)] });
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();

    // 秘书会话可见；群聊历史不成列（条目化口径）
    expect(await screen.findByTestId("panel-secretary-session")).toBeInTheDocument();
    expect(screen.queryAllByTestId("panel-project-session")).toHaveLength(0);
    // 存在群聊会话时入口保留（解散后仍可进入只读提示）
    expect(screen.getByTestId("panel-group-entry")).toBeInTheDocument();
    // 无团队引导提示已按用户要求下线
    expect(screen.queryByTestId("panel-no-team")).not.toBeInTheDocument();
    // 会话行能力不受影响（仅秘书行）
    expect(screen.getAllByTestId("panel-session-more").length).toBe(1);
  });

  it("破坏者：已挂团队但无可解析成员/员工会话 → 不得出现提醒字段（2026-10-01 拍板）", async () => {
    // 判据从「有可解析成员」改为「**是否配置了团队**」：团队已挂、员工全停用/无会话时
    // 旧实现会误报「本项目还没有团队与员工」，用户明确此场景不该提示。
    vi.mocked(companyApi.projects).mockResolvedValue({
      projects: [{ ...projectRecord(false), team_ids: ["t1"], members: [] }],
    });
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();

    expect(await screen.findByTestId("panel-secretary-session")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-no-team")).not.toBeInTheDocument();
  });

  it("会话行菜单：打开文件夹 + 置顶/归档/重命名/批量选择/删除会话（秘书行）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ sessions: panelSessions(), pinnedSessionIds: [] });
    renderPanel();

    await screen.findByTestId("panel-secretary-session");
    fireEvent.mouseEnter(
      screen.getAllByTestId("panel-secretary-session")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("panel-session-more")[0]);
    const items = await screen.findAllByRole("menuitem");
    expect(items.map((el) => el.textContent?.trim())).toEqual([
      "打开文件夹",
      "置顶",
      "归档",
      "重命名",
      "批量选择",
      "删除会话",
    ]);
    // 破坏者视角：会话行菜单不得再出现「从列表中删除」（项目级，已归顶栏）
    expect(screen.queryByText("从列表中删除")).not.toBeInTheDocument();
  });

  it("会话行菜单：秘书行提供置顶/重命名/批量选择/删除会话；群聊条目删除走确认弹窗", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ sessions: panelSessions(), pinnedSessionIds: [] });
    renderPanel();

    await screen.findByTestId("panel-secretary-session");
    fireEvent.mouseEnter(
      screen.getAllByTestId("panel-secretary-session")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("panel-session-more")[0]);
    expect(await screen.findByTestId("panel-session-pin")).toHaveTextContent("置顶");
    expect(screen.getByTestId("panel-session-rename")).toBeInTheDocument();
    expect(screen.getByTestId("panel-session-bulk")).toBeInTheDocument();
    await user.click(screen.getByTestId("panel-session-pin"));
    expect(useChatStore.getState().pinnedSessionIds).toEqual(["sec-1"]);
    await user.keyboard("{Escape}");

    // 群聊条目：右侧删除 → 确认弹窗（删除按钮悬浮才可见，先悬浮该行）
    fireEvent.mouseEnter(screen.getByTestId("panel-group-entry"));
    await user.click(screen.getByTestId("panel-group-delete"));
    expect(await screen.findByTestId("panel-session-delete-confirm")).toBeInTheDocument();
  });

  it("群聊删除：后端 404（会话本已不存在）时本地照常清理，条目不残留（2026-10-16 真机）", async () => {
    const user = userEvent.setup();
    vi.mocked(sessionsApi.remove).mockRejectedValue(new ApiError(404, "SESSION_NOT_FOUND"));
    try {
      useChatStore.setState({ sessions: panelSessions(), pinnedSessionIds: [] });
      renderPanel();

      await screen.findByTestId("panel-secretary-session");
      fireEvent.mouseEnter(screen.getByTestId("panel-group-entry"));
      await user.click(screen.getByTestId("panel-group-delete"));
      await user.click(await screen.findByTestId("panel-session-delete-confirm"));
      await waitFor(() => {
        const ids = useChatStore.getState().sessions.map((item) => item.id);
        expect(ids).not.toContain("group-1");
        expect(ids).toContain("group-2");
      });
      // 秘书会话不受群聊删除影响
      expect(useChatStore.getState().sessions.map((item) => item.id)).toContain("sec-1");
    } finally {
      vi.mocked(sessionsApi.remove).mockReset();
      vi.mocked(sessionsApi.remove).mockResolvedValue({ ok: true });
    }
  });

  it("群聊入口行：无群聊会话时不渲染（仅秘书会话 + 有团队）", async () => {
    useChatStore.setState({
      sessions: [
        { ...session("sec-1", "秘书会话一", "p1", "2026-09-21T10:00:00"), origin: "secretary" },
      ] as never,
      pinnedSessionIds: [],
    });
    renderPanel();
    await screen.findByTestId("panel-secretary-session");
    expect(screen.queryByTestId("panel-group-entry")).not.toBeInTheDocument();
  });

  it("重新拉入团队：无群聊会话时补建空群聊恢复入口行（2026-10-16 用户拍板）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      sessions: [
        { ...session("sec-1", "秘书会话一", "p1", "2026-09-21T10:00:00"), origin: "secretary" },
      ] as never,
      pinnedSessionIds: [],
    });
    renderPanel();
    await screen.findByTestId("panel-secretary-session");
    expect(screen.queryByTestId("panel-group-entry")).not.toBeInTheDocument();

    await user.click(screen.getByTestId("panel-feature-menu"));
    await user.click(await screen.findByTestId("panel-select-team"));
    await user.click(await screen.findByTestId("team-option"));
    await waitFor(() => {
      expect(vi.mocked(sessionsApi.create)).toHaveBeenCalledWith(
        expect.objectContaining({ project_id: "p1" }),
      );
    });
    await waitFor(() => {
      expect(screen.getByTestId("panel-group-entry")).toBeInTheDocument();
    });
  });

  it("会话行菜单提供归档：点击调后端归档 + 重取列表（不再本地标记隐藏）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ sessions: panelSessions(), pinnedSessionIds: [] });
    renderPanel();

    await screen.findByTestId("panel-secretary-session");
    fireEvent.mouseEnter(
      screen.getAllByTestId("panel-secretary-session")[0].parentElement as HTMLElement,
    );
    await user.click(screen.getAllByTestId("panel-session-more")[0]);
    const archiveItem = await screen.findByTestId("panel-session-archive");
    expect(archiveItem).toHaveTextContent("归档");
    await user.click(archiveItem);
    await waitFor(() => {
      expect(vi.mocked(sessionsApi.archive)).toHaveBeenCalledWith("sec-1");
    });
    // 功能：归档后重取服务端列表（active）——不再写本地 archivedSessionIds
    await waitFor(() => {
      expect(vi.mocked(sessionsApi.list)).toHaveBeenCalled();
    });
  });

  it("置顶会话排到列表最前（面板与任务区同一置顶语义）", async () => {
    useChatStore.setState({
      sessions: [
        { ...session("sec-1", "秘书会话一", "p1", "2026-09-21T10:00:00"), origin: "secretary" },
        { ...session("sec-2", "秘书会话二", "p1", "2026-09-19T10:00:00"), origin: "secretary" },
      ] as never,
      pinnedSessionIds: ["sec-2"],
    });
    renderPanel();
    const rows = await screen.findAllByTestId("panel-secretary-session");
    // sec-2 置顶 → 即使时间早于 sec-1 也排最前
    expect(rows[0]).toHaveTextContent("秘书会话二");
    expect(rows[1]).toHaveTextContent("秘书会话一");
  });

  it("批量选择覆盖秘书会话（群聊条目化后不成列，不参与批量）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();
    // 入口在秘书行 ⋯ 菜单（需展开菜单）
    // 头像行「更多」平时隐形（hover 才浮现）——先悬浮该行再点
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    await user.click(await screen.findByTestId("panel-secretary-more"));
    await user.click(await screen.findByTestId("panel-session-bulk"));
    // 全选口径 = 秘书 1（群聊条目化后不再计入）
    await user.click(screen.getByTestId("panel-bulk-all"));
    expect(screen.getByTestId("panel-bulk-bar")).toHaveTextContent("已选 1 项");
    // 再点一次全选 = 取消（toggle 语义）
    await user.click(screen.getByTestId("panel-bulk-all"));
    expect(screen.getByTestId("panel-bulk-bar")).toHaveTextContent("已选 0 项");
  });

  it("群聊条目点击：有会话 → 打开最新不重复建；无会话 → 入口行不渲染（2026-10-16 拍板）", async () => {
    const user = userEvent.setup();
    vi.mocked(sessionsApi.create).mockClear();
    // 有会话：打开最新（消息优先），不重复建
    useChatStore.setState({ sessions: panelSessions(), activeSessionId: null });
    renderPanel();
    await user.click(await screen.findByTestId("panel-group-open"));
    await waitFor(() => expect(useChatStore.getState().activeSessionId).toBe("group-1"));
    expect(vi.mocked(sessionsApi.create)).not.toHaveBeenCalled();

    // 无会话：入口行整行消失（删除群聊后语义——恢复走顶部「选择团队」重新拉入）
    useChatStore.setState({ sessions: [], activeSessionId: null });
    await waitFor(() => {
      expect(screen.queryByTestId("panel-group-entry")).not.toBeInTheDocument();
    });
  });

  it("破坏者：空置占位会话（更晚创建）不得遮蔽有消息的真实群聊（刷新后「消息没了」根因）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      sessions: [
        {
          ...session("blank-new", "项目总群聊", "p1", "2026-10-16T07:52:38"),
          message_count: 0,
        } as never,
        session("group-1", "团队会话一", "p1", "2026-10-16T07:47:51"),
      ],
      activeSessionId: null,
    });
    vi.mocked(sessionsApi.create).mockClear();
    renderPanel();
    await user.click(await screen.findByTestId("panel-group-open"));
    await waitFor(() => expect(useChatStore.getState().activeSessionId).toBe("group-1"));
    expect(vi.mocked(sessionsApi.create)).not.toHaveBeenCalled();
  });

  it("群聊会话不再出现在列表（条目化）——批量不覆盖群聊", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      sessions: [session("s1", "团队会话一", "p1"), session("s2", "团队会话二", "p1")],
    });
    renderPanel();

    // 群聊会话不成列（条目即入口）
    expect(await screen.findByTestId("panel-group-entry")).toBeInTheDocument();
    expect(screen.queryAllByTestId("panel-project-session")).toHaveLength(0);

    // 进入批量：无群聊勾选行（秘书列表为空）
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    await user.click(await screen.findByTestId("panel-secretary-more"));
    await user.click(await screen.findByTestId("panel-session-bulk"));
    expect(screen.queryAllByTestId("panel-bulk-session")).toHaveLength(0);
    expect(screen.getByTestId("panel-bulk-bar")).toHaveTextContent("已选 0 项");
  });

  it("切项目重置批量态：旧项目选中项不得残留（137b-3 修复——防误删旧项目会话）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      sessions: [
        { ...session("sec-1", "秘书会话一", "p1"), origin: "secretary" },
        { ...session("sec-2", "秘书会话二", "p1"), origin: "secretary" },
      ] as never,
    });
    renderPanelWithNav();
    // 头像行「更多」平时隐形（hover 才浮现）——先悬浮该行再点
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    await user.click(await screen.findByTestId("panel-secretary-more"));
    await user.click(await screen.findByTestId("panel-session-bulk"));
    await user.click(screen.getByTestId("panel-bulk-all"));
    expect(screen.getByTestId("panel-bulk-bar")).toHaveTextContent("已选 2 项");

    await user.click(screen.getByTestId("goto-p2"));
    await waitFor(() => {
      expect(screen.queryByTestId("panel-bulk-bar")).not.toBeInTheDocument();
    });
  });

  it("批量删除中途失败：已删项剔除、未删项保留供重试（137b-3 修复）", async () => {
    const user = userEvent.setup();
    useChatStore.setState({
      sessions: [
        { ...session("sec-1", "秘书会话一", "p1"), origin: "secretary" },
        { ...session("sec-2", "秘书会话二", "p1"), origin: "secretary" },
      ] as never,
    });
    const remove = vi.mocked(sessionsApi.remove);
    remove.mockReset();
    remove.mockResolvedValueOnce({} as never);
    remove.mockRejectedValueOnce(new Error("boom"));
    renderPanel();
    // 头像行「更多」平时隐形（hover 才浮现）——先悬浮该行再点
    fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
    await user.click(await screen.findByTestId("panel-secretary-more"));
    await user.click(await screen.findByTestId("panel-session-bulk"));
    await user.click(screen.getByTestId("panel-bulk-all"));
    await user.click(screen.getByTestId("panel-bulk-delete"));
    await user.click(await screen.findByTestId("panel-bulk-delete-confirm"));

    await waitFor(() => expect(remove).toHaveBeenCalledTimes(2));
    // 第一个成功（剔除）→ 剩 1 项选中（失败项保留，可重试）——原实现残留已删 id 致计数虚高
    await waitFor(() => {
      expect(screen.getByTestId("panel-bulk-bar")).toHaveTextContent("已选 1 项");
    });
  });

  it("搜索框常驻：聚焦后呈现历史任务（分区口径 = 任务 / 项目空间，含员工会话），输入后转为搜索结果", async () => {
    const user = userEvent.setup();
    vi.mocked(companyApi.threads).mockResolvedValue({
      threads: [
        {
          thread_id: "t-market",
          parent_task_id: null,
          participants: ["user", "市场调研"],
          created_at: "2026-09-21T10:00:00",
          message_count: 2,
        },
      ],
    });
    useChatStore.setState({
      sessions: [
        session("s1", "团队会话一", "p1", "2026-09-20T10:00:00"),
        session("s2", "竞品分析框架", "p1", "2026-09-19T10:00:00"),
        { ...session("s3", "秘书周报草稿", "p1", "2026-09-18T10:00:00"), origin: "secretary" },
        session("s4", "无项目会话", ""),
      ],
    });
    renderPanel();

    const input = await screen.findByTestId("panel-search-input");
    expect(screen.queryAllByTestId("panel-history-entry")).toHaveLength(0);
    await user.click(input);

    // 未输入 → 历史记录：大标题 + 分区子标题（助理 / 项目总群聊 / 员工会话）
    const historyRows = await screen.findAllByTestId("panel-history-entry");
    expect(historyRows).toHaveLength(3);
    expect(screen.getByText("历史任务")).toBeInTheDocument();
    expect(screen.getAllByTestId("panel-group-title").map((node) => node.textContent)).toEqual([
      "助理",
      "项目总群聊",
    ]);
    // 破坏者视角：任务区（无项目归属）会话不得进入项目面板
    expect(screen.queryByText("无项目会话")).not.toBeInTheDocument();

    // 输入 → 搜索命中，且分区子标题随命中收敛
    await user.type(input, "竞品");
    let hits = await screen.findAllByTestId("panel-search-hit");
    expect(hits).toHaveLength(1);
    expect(hits[0]).toHaveTextContent("竞品分析框架");
    expect(screen.getAllByTestId("panel-group-title").map((node) => node.textContent)).toEqual([
      "项目总群聊",
    ]);

    await user.clear(input);
    await user.type(input, "周报");
    hits = await screen.findAllByTestId("panel-search-hit");
    expect(hits).toHaveLength(1);
    expect(hits[0]).toHaveTextContent("秘书周报草稿");

    await user.clear(input);
    await user.type(input, "不存在的会话");
    expect(await screen.findByText("没有匹配的会话")).toBeInTheDocument();
    expect(screen.queryAllByTestId("panel-search-hit")).toHaveLength(0);
  });

  it("无团队：仍可从功能菜单拉入团队；中部引导提示已下线", async () => {
    const user = userEvent.setup();
    vi.mocked(companyApi.projects).mockResolvedValueOnce({ projects: [projectRecord(false)] });
    renderPanel();

    expect(screen.queryByTestId("panel-no-team")).not.toBeInTheDocument();
    expect(screen.getByTestId("panel-secretary-entry")).toBeInTheDocument();
    expect(screen.getByTestId("panel-group-entry")).toBeInTheDocument();

    await user.click(screen.getByTestId("panel-feature-menu"));
    expect(await screen.findByTestId("panel-select-team")).not.toHaveAttribute("data-disabled");
  });

  // 面板内秘书/团队会话与员工单聊都要有任务状态；「更多」改悬浮显示
  describe("头像行任务状态与悬浮更多", () => {
    it("秘书会话组有在跑 → 秘书行显示执行中转圈", async () => {
      useChatStore.setState({
        sessions: [{ ...session("sec-1", "秘书会话一", "p1"), origin: "secretary" }] as never,
        sessionActivity: { "sec-1": "running" },
      });
      renderPanel();
      expect(await screen.findByTestId("panel-secretary-activity-running")).toBeInTheDocument();
    });

    it("群聊会话组已完成 → 群聊行显示完成圆点", async () => {
      useChatStore.setState({
        sessions: [session("group-1", "团队会话一", "p1")],
        sessionActivity: { "group-1": "done" },
      });
      renderPanel();
      expect(await screen.findByTestId("panel-group-activity-done")).toBeInTheDocument();
    });

    it("头像行「更多」平时隐形（pointer-events: none），悬浮行后浮现", async () => {
      renderPanel();
      const trigger = await screen.findByTestId("panel-secretary-more");
      expect(trigger).toHaveStyle({ opacity: "0", pointerEvents: "none" });

      fireEvent.mouseEnter(screen.getByTestId("panel-secretary-entry"));
      await waitFor(() => expect(trigger).toHaveStyle({ opacity: "1" }));
      expect(trigger).toHaveStyle({ pointerEvents: "auto" });
    });
  });

  it("团队群聊头像 = 正方形轻圆角 + 最多 4 个员工头像（不再圆形图标）", async () => {
    const roleIds = ["r1", "r2", "r3", "r4", "r5"];
    vi.mocked(companyApi.projects).mockResolvedValue({
      projects: [
        {
          project_id: "p1",
          name: "选品项目",
          root_dir: "",
          team_ids: ["t1"],
          created_at: "",
          updated_at: "",
          members: roleIds.map((roleId, index) => ({
            team_id: "t1",
            role_id: roleId,
            role_name: `岗位${index + 1}`,
            employee_id: `e${index + 1}`,
            employee_name: `员工${index + 1}`,
            employee_status: "active",
          })),
        },
      ],
    } as never);
    vi.mocked(companyApi.employees).mockResolvedValue({
      employees: roleIds.map((roleId, index) => ({
        employee_id: `e${index + 1}`,
        name: `员工${index + 1}`,
        role_id: roleId,
        status: "active",
        metadata: { avatar: { kind: "tone", tone: index, shape: "classic" } },
      })),
    } as never);
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();

    const avatar = await screen.findByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "4");
    expect(avatar.className).toContain("rounded-md");
    expect(avatar.className).not.toContain("rounded-full");
    expect(screen.getAllByTestId("team-avatar-cell")).toHaveLength(4);
  });

  it("员工列表未就绪时群聊头像仍按项目成员渲染（不回落单图标）", async () => {
    const roleIds = ["r1", "r2", "r3", "r4", "r5"];
    vi.mocked(companyApi.projects).mockResolvedValue({
      projects: [
        {
          project_id: "p1",
          name: "选品项目",
          root_dir: "",
          team_ids: ["t1"],
          created_at: "",
          updated_at: "",
          members: roleIds.map((roleId, index) => ({
            team_id: "t1",
            role_id: roleId,
            role_name: `岗位${index + 1}`,
            employee_id: `e${index + 1}`,
            employee_name: `员工${index + 1}`,
            employee_status: "active",
          })),
        },
      ],
    } as never);
    vi.mocked(companyApi.employees).mockResolvedValue({ employees: [] } as never);
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();

    const avatar = await screen.findByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "4");
    expect(screen.getAllByTestId("team-avatar-cell")).toHaveLength(4);
  });

  it("删除群聊：本地条目立即消失（不等待后端返回）", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.mocked(sessionsApi.remove).mockImplementation(async () => {
      await gate;
      return { ok: true };
    });
    useChatStore.setState({
      sessions: [session("group-1", "团队会话一", "p1")],
      activeSessionId: "group-1",
    });
    renderPanel();

    const entry = await screen.findByTestId("panel-group-entry");
    fireEvent.mouseEnter(entry);
    fireEvent.click(screen.getByTestId("panel-group-delete"));
    fireEvent.click(await screen.findByTestId("panel-session-delete-confirm"));

    await waitFor(() => expect(screen.queryByTestId("panel-group-entry")).not.toBeInTheDocument(), {
      timeout: 800,
    });
    release();
    vi.mocked(sessionsApi.remove).mockResolvedValue({ ok: true });
  });

  it("团队解散后群聊入口保留，头像回退为方形图标", async () => {
    vi.mocked(companyApi.projects).mockResolvedValue({ projects: [projectRecord(false)] });
    vi.mocked(companyApi.employees).mockResolvedValue({ employees: [] });
    useChatStore.setState({ sessions: panelSessions() });
    renderPanel();

    const entry = await screen.findByTestId("panel-group-entry");
    const avatar = await screen.findByTestId("team-avatar");
    expect(avatar).toHaveAttribute("data-count", "0");
    expect(avatar.className).toContain("rounded-md");
    expect(avatar.className).not.toContain("rounded-full");
    expect(entry).toBeInTheDocument();
  });
});
