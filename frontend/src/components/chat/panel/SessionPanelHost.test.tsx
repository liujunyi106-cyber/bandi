/**
 * 功能：右栏宿主测试。
 * .2：作用域 = session:<id> / employee:<项目>::<角色>（DM 路由右栏可用）。
 */
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SessionPanelHost } from "@/components/chat/panel/SessionPanelHost";
import { useChatStore } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

vi.mock("@/services/panel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/panel")>();
  return {
    ...actual,
    panelApi: {
      ...actual.panelApi,
      files: vi.fn(async (sessionId: string, path = "") => ({
        session_id: sessionId,
        root: "C:/ws",
        path,
        entries: [],
      })),
      skills: vi.fn(async () => ({ skills: [] })),
      sop: vi.fn(async (sessionId: string) => ({ session_id: sessionId, sop: null })),
      costDetail: vi.fn(async (sessionId: string) => ({
        session_id: sessionId,
        total_tokens: 0,
        total_cost_cny: 0,
        per_model: {},
        tool_stats: {},
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
      artifacts: vi.fn(async () => ({ session_id: "s1", artifacts: [] })),
    },
  };
});

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      employees: vi.fn(async () => ({ employees: [] })),
      workItems: vi.fn(async () => ({ items: [], column_counts: {} })),
    },
  };
});

function renderHost(route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <SessionPanelHost />
    </MemoryRouter>,
  );
}

/** 功能：带导航按钮的宿主（同一 mount 内切路由——作用域切换测试）。 */
function HostWithNav() {
  const navigate = useNavigate();
  return (
    <>
      <button
        type="button"
        data-testid="go-dm"
        onClick={() => navigate("/chat?view=dm&target=dev&project=p1")}
      >
        go-dm
      </button>
      <SessionPanelHost />
    </>
  );
}

describe("SessionPanelHost", () => {
  beforeEach(() => {
    useChatStore.setState({ activeSessionId: "s1", streams: {} });
    usePanelStore.setState({
      open: false,
      fullscreen: false,
      view: "overview",
      tabs: [],
      activeTabId: "",
      fileFocusPath: "",
      expandRoleId: "",
      toolFocus: null,
      taskFocusId: "",
      dismissedAuto: {},
      autoOpened: {},
      widthBySession: {},
    });
  });

  it("收起态不渲染；展开态在会话路由渲染", () => {
    renderHost();
    expect(screen.queryByTestId("session-panel")).not.toBeInTheDocument();
    act(() => usePanelStore.getState().setOpen(true));
    expect(screen.getByTestId("session-panel")).toBeInTheDocument();
  });

  it("非会话路由不渲染（即使展开）", () => {
    usePanelStore.setState({ open: true });
    renderHost("/team");
    expect(screen.queryByTestId("session-panel")).not.toBeInTheDocument();
  });

  it("窄屏（<820px）不渲染", () => {
    const original = window.matchMedia;
    window.matchMedia = vi.fn((query: string) => ({
      matches: query.includes("max-width: 819px"),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })) as unknown as typeof window.matchMedia;
    usePanelStore.setState({ open: true });
    renderHost();
    expect(screen.queryByTestId("session-panel")).not.toBeInTheDocument();
    window.matchMedia = original;
  });

  it("自动展开：本会话流式活动 → 开一次（按作用域记忆 session: 前缀）", () => {
    useChatStore.setState({ streams: { s1: { sessionId: "s1", pendingId: "m1" } } });
    renderHost();
    expect(usePanelStore.getState().open).toBe(true);
    expect(usePanelStore.getState().autoOpened["session:s1"]).toBe(true);
    expect(usePanelStore.getState().autoOpened.s1).toBeUndefined();
    expect(screen.getByTestId("session-panel")).toBeInTheDocument();
  });

  it("DM 路由：展开态按员工单聊作用域渲染（B1.2 可见性放宽）", () => {
    usePanelStore.setState({ open: true });
    renderHost("/chat?view=dm&target=dev&project=p1");
    expect(screen.getByTestId("session-panel")).toBeInTheDocument();
  });

  it("破坏者：DM 无 target 不渲染（不回落当前会话）", () => {
    usePanelStore.setState({ open: true });
    renderHost("/chat?view=dm");
    expect(screen.queryByTestId("session-panel")).not.toBeInTheDocument();
  });

  it("会话 → DM 切作用域：清运行时态（页签）", async () => {
    usePanelStore.setState({ open: true });
    render(
      <MemoryRouter initialEntries={["/"]}>
        <HostWithNav />
      </MemoryRouter>,
    );
    act(() => usePanelStore.getState().openTab({ kind: "file", title: "a.md", path: "a.md" }));
    expect(usePanelStore.getState().tabs).toHaveLength(1);
    await userEvent.setup().click(screen.getByTestId("go-dm"));
    expect(usePanelStore.getState().tabs).toHaveLength(0);
  });

  it("切会话：清空页签与联动选中（mount 基线后新开页签 → 切会话被清）", () => {
    usePanelStore.setState({ open: true });
    renderHost();
    act(() => usePanelStore.getState().openTab({ kind: "file", title: "a.md", path: "a.md" }));
    expect(usePanelStore.getState().tabs).toHaveLength(1);
    act(() => useChatStore.setState({ activeSessionId: "s2" }));
    expect(usePanelStore.getState().tabs).toHaveLength(0);
    expect(usePanelStore.getState().activeTabId).toBe("");
  });
});
