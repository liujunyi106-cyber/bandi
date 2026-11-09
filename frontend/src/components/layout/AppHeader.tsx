/**
 * 功能：顶栏。
 * 左：收缩开关（一键全部收缩/展开：左栏 + 项目面板）+ 当前会话名（+ 重命名按钮）；
 * 右：右栏开合 icon（v5：连接灯移除，与左栏切换按钮对称；WS 异常时保留文字提示）。
 * ：通知中心移至左栏底部；顶栏标题旁加重命名；
 * 项目归属下拉已删（新建会话走项目行按钮，顶栏仅保留收缩与连接状态）。
 */
import { Pencil } from "lucide-react";
import { useState } from "react";
import { useLocation } from "react-router-dom";

import { PanelToggleIcon } from "@/components/layout/PanelToggleIcon";
import { RenameSessionDialog } from "@/components/chat/RenameSessionDialog";
import { SessionTools } from "@/components/chat/SessionTools";
import { Button } from "@/components/ui/button";
import { useProjectTeamName } from "@/hooks/use-project-team-name";
import { useT } from "@/i18n";
import { resolvePanelScope } from "@/lib/panel-scope";
import type { WsStatus } from "@/services/ws";
import { useChatStore } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";
import { useUiStore } from "@/stores/ui-store";

const WS_RANK: Record<WsStatus, number> = {
  open: 0,
  idle: 1,
  connecting: 2,
  reconnecting: 3,
  closed: 4,
};

/** 功能：两条连接取更差状态（顶栏单点指示）。 */
function worseStatus(a: WsStatus, b: WsStatus): WsStatus {
  return WS_RANK[a] >= WS_RANK[b] ? a : b;
}

/** 功能：连接状态的中文短文案；已连通时不占位（只留绿点）。 */
function wsLabelKey(status: WsStatus): string | null {
  if (status === "open" || status === "idle") return null;
  if (status === "reconnecting") return "ws.reconnecting";
  if (status === "closed") return "ws.closed";
  return "ws.connecting";
}

export function AppHeader({
  onTogglePanels,
  panelsCollapsed = false,
}: {
  onTogglePanels?: () => void;
  panelsCollapsed?: boolean;
}) {
  const t = useT();
  const location = useLocation();
  const wsStatus = useUiStore((state) => state.wsStatus);
  const sessionWsStatus = useUiStore((state) => state.sessionWsStatus);
  const headerToolItems = useUiStore((state) => state.headerToolItems);
  const headerToolTestId = useUiStore((state) => state.headerToolTestId);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const activeProjectId = useChatStore((state) => state.activeProjectId);
  const sessions = useChatStore((state) => state.sessions);
  const [renamingOpen, setRenamingOpen] = useState(false);

  const isChatRoute = location.pathname === "/" || location.pathname.startsWith("/chat");
  // 单聊路由：标题取项目所属团队名（会话标题属于群聊，直接沿用会显示「项目总群聊」）
  const searchParams = new URLSearchParams(location.search);
  const isDmView = isChatRoute && searchParams.get("view") === "dm";
  const dmProjectId = isDmView ? searchParams.get("project") || activeProjectId : "";
  const dmTeamName = useProjectTeamName(dmProjectId);
  const activeSession = sessions.find((item) => item.id === activeSessionId);
  const pageTitle = isDmView
    ? dmTeamName || t("chat.panel.groupEntry")
    : activeSession?.title?.trim() || t("nav.chat");
  const status = worseStatus(wsStatus, sessionWsStatus);
  const labelKey = wsLabelKey(status);
  // 右栏开合（v5：与左栏 SidebarTrigger 同款镜像图标；仅收起态出现——展开后收起由右栏自身负责
  // 单一开合入口、不与右栏「⇥ 收起」双按钮并存）
  // .2：作用域放宽——会话与项目内员工单聊（DM 路由）均有右栏入口
  const panelOpen = usePanelStore((state) => state.open);
  const panelScope = isChatRoute
    ? resolvePanelScope({
        search: location.search,
        sessionId: activeSessionId ?? "",
        projectId: activeProjectId,
      })
    : null;
  const showPanelTrigger = Boolean(panelScope) && !panelOpen;
  const messageCount = useChatStore(
    (state) =>
      (activeSessionId
        ? state.messagesBySession[activeSessionId]?.length
        : state.messagesBySession[""]?.length) ?? 0,
  );
  const busy = Boolean(useChatStore((state) => state.streams[activeSessionId ?? ""]));
  // 空会话（无会话/无项目/无消息/未在跑）不占顶栏高度，让对话区更干净
  const hideEmptyChatHeader =
    isChatRoute &&
    !activeSessionId &&
    !activeProjectId &&
    !location.search &&
    messageCount === 0 &&
    !busy;

  // 功能：顶栏只服务会话路由——团队/工作/知识库/技能/自动化等页面自带标题，
  // 再叠一层顶栏会出现重复页名并把内容整体下推（标题位置与其他页不一致）
  if (!isChatRoute || hideEmptyChatHeader) {
    return null;
  }

  return (
    <>
      <header
        className="bg-chat-bg flex h-14 shrink-0 items-center gap-2 px-3"
      >
        {/* 功能：项目面板收起后，顶栏出现「展开项目面板」按钮——
            普通会话（无项目归属）不显示；展开态由项目面板内按钮负责收起 */}
        {panelsCollapsed && isChatRoute && activeProjectId ? (
          <button
            type="button"
            data-testid="project-panel-expand"
            title={t("chat.panel.expand")}
            aria-label={t("chat.panel.expand")}
            className="text-muted-foreground hover:bg-primary/5 hover:text-accent-foreground inline-flex size-8 shrink-0 items-center justify-center rounded-[8px]"
            onClick={() => onTogglePanels?.()}
          >
            <PanelToggleIcon side="left" />
          </button>
        ) : null}
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="text-foreground truncate text-sm font-semibold">{pageTitle}</span>
          {isChatRoute && activeSession && !isDmView ? (
            <Button
              variant="ghost"
              size="icon-xs"
              data-testid="header-session-rename"
              title={t("chat.session.rename")}
              className="text-muted-foreground hover:text-foreground translate-y-[3px]"
              onClick={() => setRenamingOpen(true)}
            >
              <Pencil className="size-2.5" />
            </Button>
          ) : null}
        </div>

        {/* 右侧组：搜索/历史提问 + 连接提示 + 右栏开合（统一 gap-1——2026-09-26 用户反馈三类 icon 间距一致） */}
        <div className="flex shrink-0 items-center gap-1">
          {/* 2026-09-23 会话内搜索/历史提问移到顶栏，与连接状态指示同排对齐 */}
          {headerToolTestId && headerToolItems.length > 0 ? (
            <SessionTools items={headerToolItems} testId={headerToolTestId} />
          ) : null}

          {/* WS 异常提示（v5：连接灯移除，仅异常时留文字；正常态不占位） */}
          {labelKey ? (
            <div
              className="text-muted-foreground shrink-0 px-1 text-xs"
              title={t(labelKey)}
              data-testid="ws-status-label"
            >
              <span className="hidden sm:inline">{t(labelKey)}</span>
            </div>
          ) : null}

          {/* 右栏展开入口 */}
          {showPanelTrigger ? (
            <button
              type="button"
              data-testid="panel-toggle-header"
              title={t("chat.panel.expand")}
              aria-label={t("chat.panel.expand")}
              className="text-muted-foreground hover:bg-primary/5 hover:text-accent-foreground inline-flex size-8 shrink-0 items-center justify-center rounded-[8px]"
              onClick={() => usePanelStore.getState().setOpen(true)}
            >
              <PanelToggleIcon side="right" />
            </button>
          ) : null}
        </div>
      </header>

      <RenameSessionDialog
        session={renamingOpen ? (activeSession ?? null) : null}
        onOpenChange={setRenamingOpen}
      />
    </>
  );
}
