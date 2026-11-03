/**
  * 功能：单页壳（）。
 * 视图切换不换壳、不重连 WS——左栏 + 顶栏常驻，内容区由路由 Outlet 渲染（面板级 ErrorBoundary 兜底）。
 * ：项目面板通高置顶（与左栏对齐，不在顶栏下方）；顶栏收缩按钮
 * 一键「全部收缩/全部展开」（左栏 offcanvas + 项目面板同步）。
 */
import { useEffect, useState, type CSSProperties } from "react";
import { Outlet } from "react-router-dom";

import { SessionPanelHost } from "@/components/chat/panel/SessionPanelHost";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { AppHeader } from "@/components/layout/AppHeader";
import { AppSidebar } from "@/components/layout/AppSidebar";
import { CommandPalette } from "@/components/layout/CommandPalette";
import { ProjectPanel } from "@/components/layout/ProjectPanel";
import { ComputerControlBanner } from "@/components/settings/ComputerControlBanner";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";
import { useWindowWidth } from "@/hooks/use-window-width";
import { useHostEvents } from "@/hooks/use-host-events";
import { useKernelSocket } from "@/hooks/use-kernel-socket";
import { useSessionStream } from "@/hooks/use-session-stream";
import { adaptiveSidebarWidth } from "@/lib/panel-width";
import { cn } from "@/lib/utils";
import { usePanelStore } from "@/stores/panel-store";
import { useUiStore } from "@/stores/ui-store";

export function AppShell() {
  useKernelSocket();
  useSessionStream();
  useHostEvents();
  // 面板整体收缩态（左栏 offcanvas 由 SidebarProvider 管理，本状态控制项目面板）
  const [panelsCollapsed, setPanelsCollapsed] = useState(false);
  // 命令面板（-3e）：Cmd/Ctrl+K 全局唤起
  const [paletteOpen, setPaletteOpen] = useState(false);
  // 右栏 v5：全屏时占满对话列 + 右栏区域（左栏/项目面板常驻）
  const fullscreen = usePanelStore((state) => state.fullscreen);
  // 左栏展开宽度随窗口自适应（1024→184 / 1920→216；折叠 rail 64 不受影响）
  const windowWidth = useWindowWidth();
  const uiScale = useUiStore((state) => state.uiScale);
  useEffect(() => {
    // 功能：字号挡位落到根级 zoom（弹窗 Portal 在 body，挂 #root 会漏）
    document.documentElement.style.zoom = String(uiScale);
    document.documentElement.style.setProperty("--ui-scale", String(uiScale));
  }, [uiScale]);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <SidebarProvider
      defaultOpen
      style={{ "--sidebar-width": `${adaptiveSidebarWidth(windowWidth)}px` } as CSSProperties}
    >
      <AppSidebar />
      <SidebarInset className="flex h-full min-h-0 flex-row overflow-hidden">
        {/* 项目面板：通高（从视口顶部开始，与左栏对齐）；收缩按钮在面板内 */}
        <ProjectPanel
          collapsed={panelsCollapsed}
          onToggle={() => setPanelsCollapsed((value) => !value)}
        />
        <div
          className={cn(
            "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden",
            fullscreen && "hidden",
          )}
        >
          <ComputerControlBanner />
          <AppHeader
            panelsCollapsed={panelsCollapsed}
            onTogglePanels={() => setPanelsCollapsed((value) => !value)}
          />
          <div className="bg-chat-bg min-h-0 flex-1 overflow-auto">
            <ErrorBoundary>
              <Outlet />
            </ErrorBoundary>
          </div>
        </div>
        {/* 会话右栏 v5：通高第四栏（可见性/自动展开由宿主判定） */}
        <SessionPanelHost />
      </SidebarInset>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </SidebarProvider>
  );
}
