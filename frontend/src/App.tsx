/**
 * 功能：单页壳路由表（@base docs/前端设计规划.md §2 路由 / §6 导航 IA）。
 * `/` 会话默认（对话/群聊/单聊由参数分流）；`/shared/:token` 共享只读页（壳外全屏）；
 * `/team` `/work` `/knowledge` `/skills` `/automation` `/settings` 为导航路由。
 *
 * 页面按路由懒加载：会话页参与首屏，同步引入；其余页面各自成块，切换时才下载。
 * 图表/流程图类重库（recharts、@xyflow、mermaid）只被这些页面用到，懒加载后
 * 不再计入首屏解析量——进桌面端到出第一屏之间不再等它们。
 */
import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/layout/AppShell";
import { PanelToggleIcon } from "@/components/layout/PanelToggleIcon";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useT } from "@/i18n";
import ChatPage from "@/pages/chat";
import { useUiStore } from "@/stores/ui-store";

const AutomationPage = lazy(() => import("@/pages/automation"));
const KnowledgePage = lazy(() => import("@/pages/knowledge"));
const SettingsPage = lazy(() => import("@/pages/settings"));
const SharedPage = lazy(() => import("@/pages/shared"));
const SkillsPage = lazy(() => import("@/pages/skills"));
const TeamPage = lazy(() => import("@/pages/team"));
const WorkPage = lazy(() => import("@/pages/work"));

/** 功能：懒加载期间的占位——保持底色不闪白，不做转圈（切换通常毫秒级）。 */
function RouteFallback() {
  return <div className="bg-background h-full w-full" data-testid="route-fallback" />;
}

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
      <Suspense fallback={<RouteFallback />}>
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
      </Suspense>
      {/* 功能：全局提示居中于顶部——避开右上操作按钮（SOP 编辑器）与底部输入区 */}
      <Toaster position="top-center" richColors />
    </TooltipProvider>
  );
}
