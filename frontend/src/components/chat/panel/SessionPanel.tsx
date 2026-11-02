/**
  * 功能：会话右栏 v5 外壳（数字员工工作台；@base docs/前端设计规划.md）——通高第四栏。
 * 顶部条：[标题字段 ▾（作用域视图集 + 已打开的文件）] [文件页签 ×…] … [⛶全屏] [⇥收起]；
 * 内容区：基础视图（会话=概览/员工/文件/详情；员工单聊=概览/文件）或激活页签的只读预览；
 * 宽度可拖拽（按作用域持久化，双击重置）。
 */
import {
  ChevronDown,
  FileBox,
  FileText,
  Info,
  LayoutDashboard,
  Maximize2,
  Minimize2,
  Paperclip,
  Users,
  X,
} from "lucide-react";
import { useEffect, useRef } from "react";

import { PanelTabContent } from "@/components/chat/panel/FilePreview";
import { PanelToggleIcon } from "@/components/layout/PanelToggleIcon";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/i18n";
import { useWindowWidth } from "@/hooks/use-window-width";
import { adaptiveRightPanelWidth } from "@/lib/panel-width";
import { panelViewsForScope, type PanelScope } from "@/lib/panel-scope";
import { cn } from "@/lib/utils";
import { usePanelStore, type PanelTab, type PanelView } from "@/stores/panel-store";
import { DetailsView } from "@/components/chat/panel/DetailsView";
import { EmployeesView } from "@/components/chat/panel/EmployeesView";
import { FilesView } from "@/components/chat/panel/FilesView";
import { OverviewView } from "@/components/chat/panel/OverviewView";

const VIEWS: { key: PanelView; icon: typeof LayoutDashboard; labelKey: string }[] = [
  { key: "overview", icon: LayoutDashboard, labelKey: "chat.panel.viewOverview" },
  { key: "employees", icon: Users, labelKey: "chat.panel.viewEmployees" },
  { key: "files", icon: FileText, labelKey: "chat.panel.viewFiles" },
  { key: "details", icon: Info, labelKey: "chat.panel.viewDetails" },
];

const TAB_ICON: Record<PanelTab["kind"], typeof FileText> = {
  file: FileText,
  artifact: FileBox,
  attachment: Paperclip,
};

export function SessionPanel({ scope }: { scope: PanelScope }) {
  const t = useT();
  const view = usePanelStore((state) => state.view);
  const setView = usePanelStore((state) => state.setView);
  const tabs = usePanelStore((state) => state.tabs);
  const activeTabId = usePanelStore((state) => state.activeTabId);
  const activateTab = usePanelStore((state) => state.activateTab);
  const closeTab = usePanelStore((state) => state.closeTab);
  const fullscreen = usePanelStore((state) => state.fullscreen);
  const toggleFullscreen = usePanelStore((state) => state.toggleFullscreen);
  const collapseByUser = usePanelStore((state) => state.collapseByUser);
  const widthOverride = usePanelStore((state) => state.widthBySession[scope.key]);
  const setWidth = usePanelStore((state) => state.setWidth);
  const resetWidth = usePanelStore((state) => state.resetWidth);
  const windowWidth = useWindowWidth();
  // 拍板：无手动覆盖时随窗口自适应（1024→260 / 1920→400）
  const width = widthOverride ?? adaptiveRightPanelWidth(windowWidth);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const sessionId = scope.kind === "session" ? scope.sessionId : "";

  // 全屏：Esc 恢复（与再点全屏按钮等价）
  useEffect(() => {
    if (!fullscreen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") usePanelStore.getState().setFullscreen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [fullscreen]);

  // 作用域视图集（.2：员工单聊 = 概览/文件）；持久化视图越界时回落概览（不写回偏好）
  const allowedViews = panelViewsForScope(scope);
  const menuViews = VIEWS.filter((item) => allowedViews.includes(item.key));
  const activeView = allowedViews.includes(view) ? view : "overview";
  const activeTab = tabs.find((item) => item.id === activeTabId);
  const current = VIEWS.find((item) => item.key === activeView) ?? VIEWS[0];
  const CurrentIcon = current.icon;

  return (
    <aside
      data-testid="session-panel"
      style={fullscreen ? undefined : { width }}
      className={cn(
        "border-chat-hairline bg-card relative flex h-full min-h-0 shrink-0 flex-col border-l",
        fullscreen && "min-w-0 flex-1",
      )}
    >
      {/* 宽度拖拽手柄（双击重置默认宽度） */}
      {!fullscreen ? (
        <div
          data-testid="panel-resize-handle"
          role="separator"
          aria-orientation="vertical"
          className="hover:bg-primary/40 absolute -left-1 top-0 z-10 h-full w-2 cursor-col-resize"
          onPointerDown={(event) => {
            dragRef.current = { startX: event.clientX, startWidth: width };
            event.currentTarget.setPointerCapture(event.pointerId);
          }}
          onPointerMove={(event) => {
            const drag = dragRef.current;
            if (!drag) return;
            setWidth(scope.key, drag.startWidth + (drag.startX - event.clientX));
          }}
          onPointerUp={() => {
            dragRef.current = null;
          }}
          onDoubleClick={() => resetWidth(scope.key)}
        />
      ) : null}

      {/* 顶部条（h-14 与 AppHeader 同高——2026-09-26 用户真机拍板：右栏顶部与对话面板顶部对齐） */}
      <div className="border-chat-hairline flex h-14 shrink-0 items-center gap-1 border-b px-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              data-testid="panel-view-menu"
              className="text-chat-ink hover:bg-primary/5 flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-xs font-medium"
            >
              <CurrentIcon className="size-3.5 opacity-75" />
              {t(current.labelKey)}
              <ChevronDown className="text-chat-muted size-3" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-48">
            {menuViews.map((item) => {
              const Icon = item.icon;
              return (
                <DropdownMenuItem
                  key={item.key}
                  data-testid={`panel-view-${item.key}`}
                  onClick={() => setView(item.key)}
                >
                  <Icon /> {t(item.labelKey)}
                  {item.key === activeView && !activeTab ? (
                    <span className="text-chat-muted ml-auto text-[10px]">•</span>
                  ) : null}
                </DropdownMenuItem>
              );
            })}
            {tabs.length ? (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuLabel>{t("chat.panel.openFiles")}</DropdownMenuLabel>
                {tabs.map((tab) => {
                  const Icon = TAB_ICON[tab.kind];
                  return (
                    <DropdownMenuItem
                      key={tab.id}
                      data-testid={`panel-menu-tab-${tab.id}`}
                      onClick={() => activateTab(tab.id)}
                    >
                      <Icon />
                      <span className="min-w-0 flex-1 truncate">{tab.title}</span>
                    </DropdownMenuItem>
                  );
                })}
              </>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>

        {/* 文件页签条 */}
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {tabs.map((tab) => {
            const Icon = TAB_ICON[tab.kind];
            const active = tab.id === activeTabId;
            return (
              <span
                key={tab.id}
                data-testid={`panel-tab-${tab.id}`}
                className={cn(
                  "flex shrink-0 items-center rounded-md text-xs",
                  active ? "bg-primary/5 text-chat-ink" : "text-chat-muted hover:bg-primary/5",
                )}
              >
                <button
                  type="button"
                  className="flex max-w-40 items-center gap-1 py-1 pr-0.5 pl-1.5"
                  title={tab.path ?? tab.title}
                  onClick={() => activateTab(tab.id)}
                >
                  <Icon className="size-3 shrink-0 opacity-75" />
                  <span className="truncate">{tab.title}</span>
                </button>
                <button
                  type="button"
                  data-testid={`panel-tab-close-${tab.id}`}
                  className="hover:text-chat-ink px-1 py-1"
                  title={t("chat.panel.closeTab")}
                  onClick={() => closeTab(tab.id)}
                >
                  <X className="size-3" />
                </button>
              </span>
            );
          })}
        </div>

        <button
          type="button"
          data-testid="panel-fullscreen"
          className="text-chat-muted hover:bg-primary/5 hover:text-chat-ink flex size-8 shrink-0 items-center justify-center rounded-[8px]"
          title={fullscreen ? t("chat.panel.exitFullscreen") : t("chat.panel.fullscreen")}
          aria-label={fullscreen ? t("chat.panel.exitFullscreen") : t("chat.panel.fullscreen")}
          onClick={toggleFullscreen}
        >
          {fullscreen ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
        </button>
        <button
          type="button"
          data-testid="panel-collapse"
          className="text-chat-muted hover:bg-primary/5 hover:text-chat-ink flex size-8 shrink-0 items-center justify-center rounded-[8px]"
          title={t("chat.panel.collapse")}
          aria-label={t("chat.panel.collapse")}
          onClick={() => collapseByUser(scope.key)}
        >
          <PanelToggleIcon side="right" />
        </button>
      </div>

      {/* 内容区：激活页签预览 或 基础视图（作用域视图集） */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {activeTab ? (
          <PanelTabContent sessionId={sessionId} tab={activeTab} />
        ) : activeView === "overview" ? (
          <OverviewView scope={scope} />
        ) : activeView === "employees" ? (
          <EmployeesView sessionId={sessionId} />
        ) : activeView === "files" ? (
          <FilesView scope={scope} />
        ) : (
          <DetailsView sessionId={sessionId} />
        )}
      </div>
    </aside>
  );
}
