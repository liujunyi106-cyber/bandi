/**
 * 功能：全局 UI 态（zustand，@base docs/前端设计规划.md §2 状态管理 / §8 持久化）。
 * 只放 UI 全局态（语言、侧栏、WS 状态指示）+ 顶栏会话工具栏槽（会话视图发布
 * 顶栏消费的临时槽位，不持久化、不复制业务数据源）；业务数据走服务层 hooks。
 * 持久化 key 前缀 `bandi:`。
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { Locale } from "@/i18n";
import type { WsStatus } from "@/services/ws";

/** 功能：顶栏会话工具栏条目（会话视图发布的消息最小字段）。 */
export interface HeaderToolItem {
  id: string;
  role: string;
  content: string;
}

/** 功能：对话模式（后端 resolve_initial_mode 双模式；Day137b-1 Composer 切换器）。 */
export type ChatMode = "execute" | "plan";

/** 功能：项目面板宽度（拍板拖拽； 拍板自适应——override 语义）。 */
export const PROJECT_PANEL_WIDTH_DEFAULT = 224;
// 二轮：176 → 160（桌面最小窗口 1024 下给对话区让位；中文 4-5 字名仍可容纳）
export const PROJECT_PANEL_WIDTH_MIN = 160;
// 上限收到 224（= 自适应最大宽度，拖拽不再能超过自适应上限）；
// 原 320 让面板压掉对话区，无实际使用价值。上限与 DEFAULT 同值，故不再单列 MAX 常量。

/** 功能：项目面板宽度钳制（[MIN, DEFAULT]）——拖拽写入与 override 读取共用同一口径。 */
export function clampProjectPanelWidth(width: number): number {
  return Math.round(
    Math.min(PROJECT_PANEL_WIDTH_DEFAULT, Math.max(PROJECT_PANEL_WIDTH_MIN, width)),
  );
}

interface UiState {
  /** 界面语言（zh 默认 / en 兜底）。 */
  locale: Locale;
  /** 发送键行为：enter = Enter 发送 / ctrl = Ctrl+Enter 发送。 */
  sendKey: "enter" | "ctrl+enter";
  /** 界面字号比例（0.9 ~ 1.3，6 档；作用于根级 zoom）。 */
  uiScale: number;
  /** 左栏是否折叠为 64px icon rail（仅手动；窄窗不再自动折叠—— ）。 */
  sidebarCollapsed: boolean;
  /** 内核 WS 当前状态（顶栏/状态栏指示用）。 */
  wsStatus: WsStatus;
  /** 会话级 WS 状态（Day136 第二条常驻连接；顶栏并示）。 */
  sessionWsStatus: WsStatus;
  /** 对话模式（plan=只读规划 / execute=执行；持久记忆用户选择）。 */
  chatMode: ChatMode;
  /** 会话级模型覆盖（控制条；空 = 跟随全局——Day137b-2）。 */
  sessionModel: string;
  /** 会话级思考模式（控制条；空 = 关闭——Day137b-2）。 */
  sessionThinking: string;
  /**
   * 左栏「任务」段折叠态。常驻 store：左栏 offcanvas 折叠会卸载上下文列表区
   * 组件内 useState 会在重挂载时重置为展开。
   */
  sidebarTasksOpen: boolean;
  /** 左栏「项目空间」段折叠态（同上，外置防卸载重置）。 */
  sidebarProjectsOpen: boolean;
  /** 任务段「查看更多」全量展开态（同上，外置防卸载重置）。 */
  sidebarTasksAll: boolean;
  /** 项目空间段「查看更多」全量展开态（同上，外置防卸载重置）。 */
  sidebarProjectsAll: boolean;
  /** 左栏批量选择激活态（激活时隐藏底部通知/设置按钮，批量分区贴住侧栏底部——-3）。 */
  bulkSelectionActive: boolean;
  /** 顶栏会话工具栏槽：当前会话视图发布的消息 + testId（空数组=不显示）。 */
  headerToolItems: HeaderToolItem[];
  headerToolTestId: string;
  /** 项目空间数据版本（新建项目空间后自增 → 左栏项目列表重取；）。 */
  projectsVersion: number;
  /** 模型档案数据版本（设置页「应用」档案后自增 → 会话框模型控制条重取；
   * 此前只有 Composer 内模型弹窗会通知，设置页切换后控制条不刷新）。 */
  modelsVersion: number;
  /** 项目面板宽度「手动覆盖」（拖拽值；null = 窗口自适应—— 拍板）。 */
  projectPanelWidthOverride: number | null;
  /** 功能：已激活「助理」条目的项目——新建会话空态选择项目后写入（条目不再常驻）。 */
  assistantEntryProjects: string[];
  setLocale: (locale: Locale) => void;
  setSendKey: (mode: "enter" | "ctrl+enter") => void;
  setUiScale: (scale: number) => void;
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  setWsStatus: (status: WsStatus) => void;
  setSessionWsStatus: (status: WsStatus) => void;
  setChatMode: (mode: ChatMode) => void;
  setSessionModel: (model: string) => void;
  setSessionThinking: (mode: string) => void;
  toggleSidebarTasksOpen: () => void;
  toggleSidebarProjectsOpen: () => void;
  toggleSidebarTasksAll: () => void;
  toggleSidebarProjectsAll: () => void;
  setBulkSelectionActive: (active: boolean) => void;
  setHeaderTools: (items: HeaderToolItem[], testId: string) => void;
  clearHeaderTools: () => void;
  /** 功能：通知左栏项目列表重取（项目空间创建后调用）。 */
  bumpProjectsVersion: () => void;
  /** 功能：通知会话框模型控制条重取模型档案（设置页「应用」档案后调用）。 */
  bumpModelsVersion: () => void;
  /** 功能：项目面板宽度手动覆盖（拖拽写入，钳制 min/max；窗口变化不再跟随）。 */
  setProjectPanelWidth: (width: number) => void;
  /** 功能：清除手动覆盖（双击手柄）——回到「随窗口自适应」。 */
  resetProjectPanelWidth: () => void;
  /** 功能：启用某项目的「助理」条目（新建会话空态选中项目时写入）。 */
  showAssistantEntry: (projectId: string) => void;
  /** 功能：隐藏某项目的「助理」条目（秘书会话全部删除后调用）。 */
  hideAssistantEntry: (projectId: string) => void;
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      locale: "zh",
      sendKey: "enter",
      uiScale: 1,
      sidebarCollapsed: false,
      wsStatus: "idle",
      sessionWsStatus: "idle",
      // 默认「执行」模式：用户发话即干活；「计划」由用户显式切换（Shift+Tab /
      // 输入框「计划」钮）。plan 默认会让团队会话员工困在只读规划
      // （频繁 exit_plan_mode 等审批 / bash 被禁 / 空输出 needs_attention）。
      chatMode: "execute",
      sessionModel: "",
      sessionThinking: "",
      sidebarTasksOpen: true,
      sidebarProjectsOpen: true,
      sidebarTasksAll: false,
      sidebarProjectsAll: false,
      bulkSelectionActive: false,
      headerToolItems: [],
      headerToolTestId: "",
      projectsVersion: 0,
      modelsVersion: 0,
      projectPanelWidthOverride: null,
      assistantEntryProjects: [],
      setLocale: (locale) => set({ locale }),
      setSendKey: (mode) => set({ sendKey: mode }),
      setUiScale: (scale) => set({ uiScale: scale }),
      toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
      setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
      setWsStatus: (wsStatus) => set({ wsStatus }),
      setSessionWsStatus: (sessionWsStatus) => set({ sessionWsStatus }),
      setChatMode: (chatMode) => set({ chatMode }),
      setSessionModel: (sessionModel) => set({ sessionModel }),
      setSessionThinking: (sessionThinking) => set({ sessionThinking }),
      toggleSidebarTasksOpen: () => set((state) => ({ sidebarTasksOpen: !state.sidebarTasksOpen })),
      toggleSidebarProjectsOpen: () =>
        set((state) => ({ sidebarProjectsOpen: !state.sidebarProjectsOpen })),
      toggleSidebarTasksAll: () => set((state) => ({ sidebarTasksAll: !state.sidebarTasksAll })),
      toggleSidebarProjectsAll: () =>
        set((state) => ({ sidebarProjectsAll: !state.sidebarProjectsAll })),
      setBulkSelectionActive: (bulkSelectionActive) => set({ bulkSelectionActive }),
      setHeaderTools: (headerToolItems, headerToolTestId) =>
        set({ headerToolItems, headerToolTestId }),
      clearHeaderTools: () => set({ headerToolItems: [], headerToolTestId: "" }),
      bumpProjectsVersion: () => set((state) => ({ projectsVersion: state.projectsVersion + 1 })),
      bumpModelsVersion: () => set((state) => ({ modelsVersion: state.modelsVersion + 1 })),
      setProjectPanelWidth: (width) =>
        set({ projectPanelWidthOverride: clampProjectPanelWidth(width) }),
      resetProjectPanelWidth: () => set({ projectPanelWidthOverride: null }),
      showAssistantEntry: (projectId) =>
        set((state) => ({
          assistantEntryProjects: state.assistantEntryProjects.includes(projectId)
            ? state.assistantEntryProjects
            : [...state.assistantEntryProjects, projectId],
        })),
      hideAssistantEntry: (projectId) =>
        set((state) => ({
          assistantEntryProjects: state.assistantEntryProjects.filter((item) => item !== projectId),
        })),
    }),
    {
      name: "bandi:ui",
      // v2：模式默认 =「计划」（未选中），切换后 =「执行」——迁移把旧持久化状态重置回计划默认
      // v3：项目面板宽度改「手动覆盖」模型（null = 随窗口自适应）——旧固定宽度作废
      // v4：模式默认改「执行」——v3 及以前的 plan 默认是系统给的（非用户主动选择），
      // 升级时统一重置为执行，恢复被困在只读规划的会话
      // v5：助理条目启用项目不再持久化——清掉旧持久化标记（删光秘书会话后条目仍常驻）
      version: 5,
      migrate: (persistedState, version) => {
        const persisted = (persistedState ?? {}) as Record<string, unknown>;
        const { projectPanelWidth: _legacyWidth, assistantEntryProjects: _legacyEntry, ...rest } =
          persisted;
        return {
          ...rest,
          ...(version < 4 ? { chatMode: "execute" } : {}),
          ...(version < 5 ? { assistantEntryProjects: [] } : {}),
          projectPanelWidthOverride: null,
        } as unknown as {
          locale: Locale;
          sidebarCollapsed: boolean;
          chatMode: ChatMode;
        };
      },
      partialize: (state) => ({
        locale: state.locale,
        sendKey: state.sendKey,
        uiScale: state.uiScale,
        sidebarCollapsed: state.sidebarCollapsed,
        chatMode: state.chatMode,
        // 会话级模型/思考覆盖**不再持久化**——语义是「仅本会话生效」
        // 此前被 persist 后跨会话/刷新残留（用户在一个会话切了模型，其他会话也被改）。
        sidebarTasksOpen: state.sidebarTasksOpen,
        sidebarProjectsOpen: state.sidebarProjectsOpen,
        sidebarTasksAll: state.sidebarTasksAll,
        sidebarProjectsAll: state.sidebarProjectsAll,
        // 项目面板宽度手动覆盖（null = 自适应；拖过的手动值跨会话保留）
        projectPanelWidthOverride: state.projectPanelWidthOverride,
        // 助理条目启用项目不持久化——刷新后按「是否存在秘书会话」重新判定
        // （否则删光会话后条目仍常驻）
      }),
    },
  ),
);
