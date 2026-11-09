/**
 * 功能：面板宽度随窗口自适应。
 * 锚点：桌面最小窗口 1024 → 最小值；全屏 1920 → 默认值；中间线性插值，两端 clamp。
 * 窄窗（<1024）左栏**保持展开**并继续缩窄，到 640 触底 152（不再回落 184
 * 也不折叠为 rail——原 980px 抽屉分支会让左栏整体消失，见 sidebar.tsx 口径）。
 * 用户手动拖拽 = override（持久化）覆盖自适应；双击手柄清 override 回自适应（见各面板组件）。
 */
import { PANEL_WIDTH_DEFAULT, PANEL_WIDTH_MIN } from "@/stores/panel-store";
import { PROJECT_PANEL_WIDTH_DEFAULT, PROJECT_PANEL_WIDTH_MIN } from "@/stores/ui-store";

export const ADAPTIVE_WINDOW_MIN = 1024;
export const ADAPTIVE_WINDOW_FULL = 1920;
/** 窄窗触底窗口宽（<1024 段的下端锚点）。 */
export const ADAPTIVE_WINDOW_TIGHT = 640;

/** 功能：左栏展开宽度锚点（1024→184 / 1920→216；折叠 rail 64px 不受影响）。 */
export const SIDEBAR_WIDTH_MIN = 184;
export const SIDEBAR_WIDTH_DEFAULT = 216;
/** 功能：左栏窄窗触底宽度（152——13px 四汉字 + 16px 图标 + 间距内边距仍可容纳）。 */
export const SIDEBAR_WIDTH_TIGHT = 152;

function interpolate(windowWidth: number, min: number, max: number): number {
  const span = ADAPTIVE_WINDOW_FULL - ADAPTIVE_WINDOW_MIN;
  const ratio = Math.min(1, Math.max(0, (windowWidth - ADAPTIVE_WINDOW_MIN) / span));
  return Math.round(min + ratio * (max - min));
}

/** 功能：右栏自适应宽度（1024→260 / 1920→400）。 */
export function adaptiveRightPanelWidth(windowWidth: number): number {
  return interpolate(windowWidth, PANEL_WIDTH_MIN, PANEL_WIDTH_DEFAULT);
}

/** 功能：项目面板自适应宽度（1024→160 / 1920→224）。 */
export function adaptiveProjectPanelWidth(windowWidth: number): number {
  return interpolate(windowWidth, PROJECT_PANEL_WIDTH_MIN, PROJECT_PANEL_WIDTH_DEFAULT);
}

/**
 * 功能：左栏自适应展开宽度（1920→216 / 1024→184 / 640→152 / 更窄触底 152）。
 * 窄窗不折叠、不隐藏——只缩窄，保证左侧导航在任何窗口尺寸下都在。
 */
export function adaptiveSidebarWidth(windowWidth: number): number {
  if (windowWidth >= ADAPTIVE_WINDOW_FULL) return SIDEBAR_WIDTH_DEFAULT;
  if (windowWidth >= ADAPTIVE_WINDOW_MIN) {
    return interpolate(windowWidth, SIDEBAR_WIDTH_MIN, SIDEBAR_WIDTH_DEFAULT);
  }
  if (windowWidth <= ADAPTIVE_WINDOW_TIGHT) return SIDEBAR_WIDTH_TIGHT;
  const span = ADAPTIVE_WINDOW_MIN - ADAPTIVE_WINDOW_TIGHT;
  const ratio = (windowWidth - ADAPTIVE_WINDOW_TIGHT) / span;
  return Math.round(SIDEBAR_WIDTH_TIGHT + ratio * (SIDEBAR_WIDTH_MIN - SIDEBAR_WIDTH_TIGHT));
}
