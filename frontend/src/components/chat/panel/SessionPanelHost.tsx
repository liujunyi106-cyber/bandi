/**
  * 功能：会话右栏 v5 宿主（）——通高第四栏（左栏｜项目面板｜对话｜右栏）。
 * 职责：作用域解析（.2：session:<会话id> / employee:<项目>::<角色>）+ 可见性判定 +
 * <820px 不渲染 + 切作用域清运行时态 + 首个活动自动展开（按作用域记忆）
 * + 不可见时退出全屏（防布局留白）。
 */
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";

import { SessionPanel } from "@/components/chat/panel/SessionPanel";
import { resolvePanelScope } from "@/lib/panel-scope";
import { useChatStore } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

export function SessionPanelHost() {
  const location = useLocation();
  const sessionId = useChatStore((state) => state.activeSessionId);
  const projectId = useChatStore((state) => state.activeProjectId);
  const streams = useChatStore((state) => state.streams);
  const open = usePanelStore((state) => state.open);
  const [narrow, setNarrow] = useState(
    () => typeof window !== "undefined" && window.innerWidth < 820,
  );

  useEffect(() => {
    const query = window.matchMedia("(max-width: 819px)");
    const onChange = () => setNarrow(query.matches);
    onChange();
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const isChatRoute = location.pathname === "/" || location.pathname.startsWith("/chat");
  const scope = isChatRoute
    ? resolvePanelScope({ search: location.search, sessionId: sessionId ?? "", projectId })
    : null;
  const scopeKey = scope?.key ?? "";
  const scopeKind = scope?.kind ?? "";
  const scopeSessionId = scope?.kind === "session" ? scope.sessionId : "";
  const visible = Boolean(scopeKey) && open && !narrow;

  // 切作用域：清页签与联动选中（文件/故障详情属于当前作用域）
  useEffect(() => {
    usePanelStore.getState().resetForSession();
  }, [scopeKey]);

  // 首个活动自动展开一次（会话作用域；用户关过不再弹）
  const scopedStreamActive = Boolean(scopeSessionId && streams[scopeSessionId]);
  useEffect(() => {
    if (scopeKind !== "session") return;
    if (scopedStreamActive) {
      usePanelStore.getState().maybeAutoOpen(scopeKey);
    }
  }, [scopeKey, scopeKind, scopedStreamActive]);

  // 隐藏时退出全屏（窄屏/切非会话路由/收起——防中间列被隐藏而右栏不渲染的空白）
  useEffect(() => {
    if (!visible && usePanelStore.getState().fullscreen) {
      usePanelStore.getState().setFullscreen(false);
    }
  }, [visible]);

  if (!visible || !scope) return null;
  return <SessionPanel scope={scope} />;
}
