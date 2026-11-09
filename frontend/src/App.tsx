/**
 * 功能：单页壳路由表。
 * `/` 会话默认（对话/群聊/单聊由参数分流）；`/shared/:token` 共享只读页（壳外全屏）；
 * `/team` `/work` `/knowledge` `/skills` `/automation` `/settings` 为导航路由。
 */
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { PanelToggleIcon } from "@/components/layout/PanelToggleIcon";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useT } from "@/i18n";
import AutomationPage from "@/pages/automation";
import ChatPage from "@/pages/chat";
import KnowledgePage from "@/pages/knowledge";
import SettingsPage from "@/pages/settings";
import SharedPage from "@/pages/shared";
import SkillsPage from "@/pages/skills";
import TeamPage from "@/pages/team";
import WorkPage from "@/pages/work";
import { useUiStore } from "@/stores/ui-store";

/** 功能：桌面壳标题栏拖拽区（44px）；左侧放左侧导航伸缩按钮，右侧由系统绘制窗口按钮。 */
function TitleBar() {
  const t = useT();
  const collapsed = useUiStore((state) => state.sidebarCollapsed);
  const toggleSidebar = useUiStore((state) => state.toggleSidebar);
  const label = t(collapsed ? "nav.expand" : "nav.collapse");
  return (
    <div className="titlebar-drag">
      <button
        type="button"
        data-testid="nav-toggle-titlebar"
        title={label}
        aria-label={label}
        className="titlebar-drag-button text-muted-foreground hover:bg-primary/5 hover:text-accent-foreground inline-flex size-7 items-center justify-center rounded-[6px]"
        onClick={toggleSidebar}
      >
        <PanelToggleIcon side="left" />
      </button>
    </div>
  );
}

export default function App() {
  return (
    <TooltipProvider delayDuration={200}>
      {/* 功能：桌面壳 44px 标题栏（CSS 只在 .desktop-shell 下显示） */}
      <TitleBar />
      <Routes>
        <Route path="/shared/:token" element={<SharedPage />} />
        <Route element={<AppShell />}>
          <Route index element={<ChatPage />} />
          <Route path="chat" element={<ChatPage />} />
          <Route path="team" element={<TeamPage />} />
          <Route path="work" element={<WorkPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="skills" element={<SkillsPage />} />
          <Route path="automation" element={<AutomationPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
      {/* 功能：全局提示居中于顶部——避开右上操作按钮（SOP 编辑器）与底部输入区 */}
      <Toaster position="top-center" richColors />
    </TooltipProvider>
  );
}
