/**
 * 功能：左栏导航注册表。
 * 新页/新面板只往这里加一条，AppShell 不动——插件可注入条目。
 * ：移除「会话」项（默认首页 + 会话列表已在导航下方，入口冗余；
 * Logo 点击回会话首页）。
 */
import {
  BookOpen,
  LayoutDashboard,
  Puzzle,
  Settings,
  UsersRound,
  Workflow,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  /** 路由段 + 菜单 key（`/` 用空串）。 */
  path: string;
  /** i18n 文案 key。 */
  labelKey: string;
  icon: LucideIcon;
  /** 该入口的内容区是否带「上下文列表区」（会话列表 Tab：项目 | 单聊）。 */
  contextList?: "sessions";
  /** 插件注入的条目（不在核心 nav 内维护）。 */
  pluginOwned?: boolean;
}

/** 主导航区（自上而下：团队 · 工作 · 知识 · 技能市场 · 自动化；会话为默认首页不在列）。
 *
 *：图标不统一、不好看 → 换成**视觉密度一致、块面感强**的一组（参照
 * datafoundry 的做法：左栏图标全为极简几何、统一 16px + 细描边；旧图标 Handshake/Sparkles/
 * CalendarClock/Brain 笔画细碎，12–16px 下糊成一团）。
 */
export const PRIMARY_NAV: NavItem[] = [
  // 团队用 UsersRound：与项目面板标题栏「功能」（选择团队/员工入口，`ProjectPanel.tsx:465`）
  // 同一个图标—— 「团队 icon 与项目面板不一致，以项目面板为基准」
  { path: "/team", labelKey: "nav.team", icon: UsersRound },
  { path: "/work", labelKey: "nav.work", icon: LayoutDashboard },
  { path: "/knowledge", labelKey: "nav.knowledge", icon: BookOpen },
  { path: "/skills", labelKey: "nav.skills", icon: Puzzle },
  { path: "/automation", labelKey: "nav.automation", icon: Workflow },
];

/** 底部区（⚙ 设置—— 改弹窗；「当前员工」切换器已于 删除）。 */
export const FOOTER_NAV: NavItem[] = [
  { path: "/settings", labelKey: "nav.settings", icon: Settings },
];
