/**
 * 功能：左栏窄窗常驻行为契约（缩小窗口左侧导航不得消失）。
 *
 * 背景：原实现 980px 断点（use-mobile.ts）让 Sidebar 走**抽屉**分支（fixed + 滑出）
 * `showContextList` 又额外被 `!isMobile` 关闭 —— 窗口 <980px 时左栏整体消失、只剩会话区
 * 与「桌面产品不做移动端适配」的设计口径矛盾。本文件钉死新承诺：
 * ① 窄窗下左栏仍占据布局宽度（`sidebar-gap` 占位恒在，不退化为覆盖式抽屉）；
 * ② 窄窗下上下文列表区照常渲染（不再被 isMobile 一刀切关闭）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppSidebar } from "@/components/layout/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { sessionsApi } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";

const { deleteProject, openFolder } = vi.hoisted(() => ({
  deleteProject: vi.fn(async () => ({ deleted: true })),
  openFolder: vi.fn(async () => ({ opened: "C:/work" })),
}));

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      projects: vi.fn(async () => ({ projects: [] })),
      deleteProject,
      openFolder,
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      list: vi.fn(async () => [
        {
          id: "s1",
          title: "窄窗也要看到的会话",
          created_at: "2026-10-05T10:00:00",
          message_count: 1,
          project_id: "",
        },
      ]),
      remove: vi.fn(async () => ({})),
    },
  };
});

/** 功能：设定窗口宽度（useIsMobile / useWindowWidth 均读 window.innerWidth）。 */
function setWindowWidth(width: number) {
  Object.defineProperty(window, "innerWidth", { value: width, configurable: true, writable: true });
}

function renderSidebar() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <SidebarProvider>
        <AppSidebar />
      </SidebarProvider>
    </MemoryRouter>,
  );
}

describe("左栏窄窗常驻", () => {
  beforeEach(() => {
    vi.mocked(sessionsApi.list).mockClear();
    useChatStore.setState({
      sessions: [],
      sessionsLoaded: true,
      pinnedSessionIds: [],
      activeSessionId: null,
      activeProjectId: "",
    });
  });

  afterEach(() => {
    setWindowWidth(1024);
  });

  it("破坏者：窄窗（800px）左栏仍常驻在位，不退化为滑出抽屉", () => {
    setWindowWidth(800);
    const { container } = renderSidebar();
    // 常驻模式（collapsible=none）：sidebar 槽位直接渲染，无移动端抽屉
    expect(container.querySelector('[data-slot="sidebar"]')).toBeInTheDocument();
    expect(container.querySelector('[data-slot="sidebar"][data-mobile="true"]')).toBeNull();
    // 左栏常驻：不再渲染收缩按钮（伸缩改由项目面板负责）
    expect(screen.queryByTestId("sidebar-collapse")).not.toBeInTheDocument();
  });

  it("窄窗（800px）上下文列表区照常渲染会话条目（不被 isMobile 一刀切关闭）", async () => {
    setWindowWidth(800);
    renderSidebar();
    await waitFor(() => expect(screen.getByText("窄窗也要看到的会话")).toBeInTheDocument());
  });

  it("宽窗（1440px）行为不回退：侧栏槽位 + 列表区同样在", async () => {
    setWindowWidth(1440);
    const { container } = renderSidebar();
    expect(container.querySelector('[data-slot="sidebar"]')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("窄窗也要看到的会话")).toBeInTheDocument());
  });
});
