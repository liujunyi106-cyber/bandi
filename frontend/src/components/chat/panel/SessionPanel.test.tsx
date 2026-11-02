/**
 * 功能：右栏 v5 外壳测试（视图切换下拉 / 页签开合 / 收起 / 全屏 / 宽度重置——@base §8）。
 * .2：作用域视图集（员工单聊 = 概览/文件）；宽度/收起记忆按作用域键。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SessionPanel } from "@/components/chat/panel/SessionPanel";
import type { PanelScope } from "@/lib/panel-scope";
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
      employeeKnowledge: vi.fn(async (employeeId: string) => ({
        employee_id: employeeId,
        bases: [],
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

function renderPanel(scope: PanelScope = SESSION_SCOPE) {
  return render(
    <MemoryRouter>
      <SessionPanel scope={scope} />
    </MemoryRouter>,
  );
}

describe("SessionPanel (v5)", () => {
  beforeEach(() => {
    usePanelStore.setState({
      open: true,
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

  it("标题字段下拉切视图（默认概览 → 文件）", async () => {
    const user = userEvent.setup();
    renderPanel();
    expect(screen.getByTestId("panel-overview")).toBeInTheDocument();
    await user.click(screen.getByTestId("panel-view-menu"));
    await user.click(await screen.findByTestId("panel-view-files"));
    expect(usePanelStore.getState().view).toBe("files");
    expect(await screen.findByTestId("panel-files")).toBeInTheDocument();
  });

  it("文件页签：打开显示 + × 关闭回到基础视图", async () => {
    usePanelStore.getState().focusFile("docs/a.md");
    renderPanel();
    expect(screen.getByTestId("panel-tab-file:docs/a.md")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("panel-tab-close-file:docs/a.md"));
    expect(usePanelStore.getState().activeTabId).toBe("");
    expect(screen.queryByTestId("panel-tab-file:docs/a.md")).not.toBeInTheDocument();
  });

  it("收起按钮：记入按作用域的「用户关过」（自动展开不再弹）", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByTestId("panel-collapse"));
    expect(usePanelStore.getState().open).toBe(false);
    expect(usePanelStore.getState().dismissedAuto["session:s1"]).toBe(true);
  });

  it("全屏：切换 + Esc 恢复", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByTestId("panel-fullscreen"));
    expect(usePanelStore.getState().fullscreen).toBe(true);
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(usePanelStore.getState().fullscreen).toBe(false));
  });

  it("破坏者：宽度手柄双击清除手动覆盖（2026-10-04：回窗口自适应，按作用域键）", () => {
    usePanelStore.getState().setWidth("session:s1", 500);
    renderPanel();
    fireEvent.doubleClick(screen.getByTestId("panel-resize-handle"));
    expect(usePanelStore.getState().widthBySession["session:s1"]).toBeUndefined();
  });

  it("无手动覆盖时宽度随窗口自适应（jsdom 窗口 1024 → 260）", () => {
    renderPanel();
    expect(screen.getByTestId("session-panel").style.width).toBe("260px");
  });

  it("员工作用域视图集：下拉仅概览/文件（无员工/详情）", async () => {
    const user = userEvent.setup();
    renderPanel(EMPLOYEE_SCOPE);
    await user.click(screen.getByTestId("panel-view-menu"));
    expect(await screen.findByTestId("panel-view-overview")).toBeInTheDocument();
    expect(screen.getByTestId("panel-view-files")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-view-employees")).not.toBeInTheDocument();
    expect(screen.queryByTestId("panel-view-details")).not.toBeInTheDocument();
  });

  it("破坏者：员工作用域 + 持久化 view=details（越界）→ 回落概览渲染", async () => {
    usePanelStore.setState({ view: "details" });
    renderPanel(EMPLOYEE_SCOPE);
    expect(await screen.findByTestId("panel-overview")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-details")).not.toBeInTheDocument();
  });
});
