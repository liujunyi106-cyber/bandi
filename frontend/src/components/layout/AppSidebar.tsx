/**
  * 功能：一体左栏（@base docs/前端设计规划.md 左栏结构 / 侧栏导航 IA）。
 * 结构：顶部（Logo（点击回会话首页）+ ＋新建会话）→ 导航区（团队…记忆）→ 上下文列表区
 * （仅会话页展开态：项目 | 单聊 两 Tab）→ 底部（🔔 通知〔含团队记忆待确认〕+ ⚙ 设置）。
 * ：「会话」导航项删除（首页即会话）；设置改弹窗。
 */
import { Settings } from "lucide-react";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";

import { PRIMARY_NAV, type NavItem } from "@/app/nav-registry";
import { NewSessionIcon } from "@/components/icons/NewSessionIcon";
import { NotificationMenu } from "@/components/layout/NotificationMenu";
import { SettingsDialog } from "@/components/layout/SettingsDialog";
import { SidebarContextList } from "@/components/layout/SidebarContextList";
import { Button } from "@/components/ui/button";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { useWindowWidth } from "@/hooks/use-window-width";
import { useT } from "@/i18n";
import { startNewSession } from "@/lib/new-session";
import { adaptiveSidebarWidth, SIDEBAR_WIDTH_DEFAULT } from "@/lib/panel-width";
import { cn } from "@/lib/utils";
import { useUiStore } from "@/stores/ui-store";

function NavEntries({ items }: { items: NavItem[] }) {
  const t = useT();
  const location = useLocation();
  return (
    <SidebarMenu>
      {items.map((item) => {
        // className 改为字符串（Slot/asChild 不兼容函数式 className——
        // 旧实现把函数源码拼进 class 且 isActive 样式从未生效）；active 由 location 精确计算
        const isActive =
          item.path === "/" ? location.pathname === "/" : location.pathname.startsWith(item.path);
        return (
          <SidebarMenuItem key={item.path}>
            <SidebarMenuButton asChild tooltip={t(item.labelKey)}>
              <Link
                to={item.path}
                className={cn(
                  // !important 覆盖 SidebarMenuButton variant 的 text-sidebar-foreground（#111 过深）
                  // v5：导航项与「任务/项目空间」段标题同字号（13px——见 sidebar 变体）
                  "flex items-center gap-2 text-muted-foreground!",
                  // 选中态 = 悬停态（同底色黑字）
                  isActive && "bg-primary/5 text-sidebar-accent-foreground!",
                )}
              >
                {/* 2026-09-26 用户反馈：图标统一细描边（1.75，对齐顶栏左伸缩按钮的线感/参考源
                    datafoundry 左栏 16px@1.6 的做法），避免 lucide 默认 2px 在 16px 下显糊 */}
                <item.icon strokeWidth={1.75} />
                <span>{t(item.labelKey)}</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        );
      })}
    </SidebarMenu>
  );
}

export function AppSidebar() {
  const t = useT();
  const navigate = useNavigate();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const bulkSelectionActive = useUiStore((store) => store.bulkSelectionActive);
  const windowWidth = useWindowWidth();
  const brandScale = adaptiveSidebarWidth(windowWidth) / SIDEBAR_WIDTH_DEFAULT;
  const brandFontSize = Math.max(12, Math.round(15 * brandScale));
  const brandLogoSize = Math.max(12, Math.round(16 * brandScale));

  /** 功能：新建会话（归属分派提至 lib/new-session——顶栏收起态按钮共用； 第五轮）。 */
  const newSession = () => startNewSession(navigate);

  return (
    <Sidebar collapsible="none" className="border-sidebar-border border-r">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-1 py-1">
          <Link to="/" className="flex min-w-0 flex-1 items-end gap-1" title={t("nav.chat")}>
            <img
              src={`${import.meta.env.BASE_URL}logo.svg`}
              alt=""
              className="shrink-0"
              style={{ width: brandLogoSize, height: brandLogoSize }}
            />
            <span
              style={{ fontFamily: "Eavm", fontSize: brandFontSize }}
              className="text-foreground translate-y-[2px] truncate leading-none font-semibold"
            >
              {t("app.name")}
            </span>
          </Link>
        </div>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            {/* v5（2026-09-26 用户拍板）：新建会话改为与导航项完全同款（字重/行距/间距一致，
                原主色大按钮移除）。
                2026-09-26 三轮反馈：图标「不要太方、也不用那么圆」——lucide 的
                `MessageSquarePlus` 圆角仅 r=2（显方），`MessageCirclePlus` 是正圆（太圆），
                都不合 → 自绘圆角气泡（r=4.5 + 开口尾巴 + 加号）。
                2026-09-26 五轮（设计统一）：图形抽到 `components/icons/NewSessionIcon`，
                与项目面板头像行的「＋新建会话」共用同一组件（同语义同图形） */}
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  data-testid="sidebar-new-session"
                  onClick={newSession}
                  tooltip={t("nav.newTask")}
                  className="text-muted-foreground!"
                >
                  <NewSessionIcon />
                  <span>{t("nav.newTask")}</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
            <NavEntries items={PRIMARY_NAV} />
          </SidebarGroupContent>
        </SidebarGroup>
        <SidebarContextList />
      </SidebarContent>

      <SidebarFooter>
        {/* 2026-09-25 用户拍板：批量选择激活时隐藏通知/设置——批量分区接管侧栏底部并贴底 */}
        {bulkSelectionActive ? null : (
          <div className="flex items-center justify-end gap-1">
            <NotificationMenu />
            <Button
              variant="ghost"
              size="icon-sm"
              className="no-focus-ring"
              title={t("nav.settings")}
              onClick={() => setSettingsOpen(true)}
            >
              {/* 2026-09-26 设计统一：底栏图标也收细到 1.75，与导航项一致 */}
              <Settings strokeWidth={1.75} />
            </Button>
          </div>
        )}
      </SidebarFooter>

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
    </Sidebar>
  );
}
