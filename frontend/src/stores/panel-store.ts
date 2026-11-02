/**
  * 功能：会话右栏 v5 状态（数字员工工作台；@base docs/前端设计规划.md）。
 * 集中管理：开合 / 全屏 / 四视图 / 文件页签 / 联动选中 / 宽度（按作用域）/ 自动展开记忆（按作用域）。
 * 页签与联动选中为运行时态（切作用域由 resetForSession 清空）；开合/视图/宽度/记忆持久化（bandi:panel）。
 * 作用域键（.2）：`session:<会话id>` / `employee:<项目>::<角色>`（见 lib/panel-scope.ts）。
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

/** 功能：右栏基础视图（标题字段下拉四项）。 */
export type PanelView = "overview" | "employees" | "files" | "details";

/** 功能：可打开页签的三种对象（文件 / 会话制品 / 对话附件）。 */
export type PanelTabKind = "file" | "artifact" | "attachment";

export interface PanelTab {
  id: string;
  kind: PanelTabKind;
  title: string;
  /** file/artifact：文件根内相对路径（file 页签必填；artifact 供文件树定位）。 */
  path?: string;
  /** file 页签：项目文件树来源——预览走项目端点；缺省 = 会话文件根。 */
  projectId?: string;
  /** artifact 页签：制品 id（内容预览走 artifacts/{id}/content）。 */
  artifactId?: string;
  /** attachment 页签：上传件 id（内容走 company/uploads/{id}）。 */
  uploadId?: string;
}

/** 功能：工具节点详情联动（——失败工具节点 → 详情视图）。 */
export interface ToolFocus {
  messageId: string;
  nodeId: string;
}

/** 功能：右栏宽度边界（自适应锚点 + 拖拽钳制；：min=窗口最小锚点，default=全屏锚点）。 */
export const PANEL_WIDTH_DEFAULT = 400;
// 小窗口下最小宽从 320 → 280 → 260（桌面最小窗口 1024 下给对话区让位）
export const PANEL_WIDTH_MIN = 260;
export const PANEL_WIDTH_MAX = 640;

/** 功能：同时打开的页签上限（超出时淘汰最早的非激活页签）。 */
const MAX_TABS = 8;

function makeTabId(tab: Omit<PanelTab, "id">): string {
  // 文件页签 id 并入项目维度（同路径在不同项目下互不顶替）
  if (tab.kind === "file") {
    return `file:${tab.projectId ? `${tab.projectId}::` : ""}${tab.path ?? tab.title}`;
  }
  if (tab.kind === "artifact") return `artifact:${tab.artifactId ?? tab.title}`;
  return `attachment:${tab.uploadId ?? tab.title}`;
}

interface PanelState {
  /** 右栏开合（全局；持久化——v5 取代 -3d 的 ui-store sessionPanelOpen）。 */
  open: boolean;
  /** 全屏（占满对话列 + 右栏区域；Esc/再点恢复；运行时态）。 */
  fullscreen: boolean;
  /** 基础视图（标题字段下拉；持久化用户偏好）。 */
  view: PanelView;
  /** 打开的页签（运行时态；多文件可同时打开）。 */
  tabs: PanelTab[];
  /** 激活页签（空串 = 显示基础视图）。 */
  activeTabId: string;
  /** 文件树定位高亮目标（文件打开——文件视图自动展开并高亮该路径）。 */
  fileFocusPath: string;
  /** 员工视图展开的人（——@员工 → 员工视图展开该人）。 */
  expandRoleId: string;
  /** 详情视图联动选中。 */
  toolFocus: ToolFocus | null;
  /** 概览视图任务定位高亮（——任务条 → 概览定位）。 */
  taskFocusId: string;
  /** 用户手动收起过的会话（自动展开不再弹——按作用域记忆）。 */
  dismissedAuto: Record<string, true>;
  /** 已自动展开过的会话（首个活动只自动开一次——按作用域记忆）。 */
  autoOpened: Record<string, true>;
  /** 右栏宽度手动覆盖（按作用域；无键 = 随窗口自适应；双击手柄清除覆盖）。 */
  widthBySession: Record<string, number>;
  setOpen: (open: boolean) => void;
  /** 用户主动收起（记入 dismissedAuto——此后该会话不再自动展开）。 */
  collapseByUser: (sessionId: string) => void;
  setFullscreen: (fullscreen: boolean) => void;
  toggleFullscreen: () => void;
  setView: (view: PanelView) => void;
  openTab: (tab: Omit<PanelTab, "id">) => void;
  closeTab: (id: string) => void;
  activateTab: (id: string) => void;
  /** 对话附件 → 页签。 */
  focusAttachment: (uploadId: string, title: string) => void;
  /** /文件树：打开文件页签 + 文件视图定位高亮（projectId 存在 = 项目文件树来源）。 */
  focusFile: (path: string, title?: string, projectId?: string) => void;
  /** 制品 → 页签（内容走 artifactContent）；有 path 时同文件树定位。 */
  focusArtifact: (artifactId: string, title: string, path?: string) => void;
  /** @员工 → 员工视图展开该人。 */
  focusEmployee: (roleId: string) => void;
  /** 工具节点 → 详情视图。 */
  focusTool: (messageId: string, nodeId: string) => void;
  /** 工具节点不可达（新轮次/切会话）→ 清空详情联动。 */
  clearToolFocus: () => void;
  /** 任务 → 概览视图定位。 */
  focusTask: (taskId: string) => void;
  /** 首个活动自动展开（仅一次；用户关过不再弹）。 */
  maybeAutoOpen: (sessionId: string) => void;
  /** 切会话：清空运行时态（页签/联动选中）。 */
  resetForSession: () => void;
  setWidth: (sessionId: string, width: number) => void;
  /** 功能：清除该作用域的手动覆盖（双击手柄）——回到「随窗口自适应」。 */
  resetWidth: (sessionId: string) => void;
}

/** 功能：持久化作用域键前缀（.2）——旧版裸会话键迁移为 session: 前缀；已有前缀保留。 */
function prefixScopeKeys(record: unknown): unknown {
  if (!record || typeof record !== "object" || Array.isArray(record)) return record;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record as Record<string, unknown>)) {
    out[key.startsWith("session:") || key.startsWith("employee:") ? key : `session:${key}`] = value;
  }
  return out;
}

export function migratePanelPersisted(persisted: unknown, version: number): unknown {
  if (!persisted || typeof persisted !== "object") return persisted;
  const state = persisted as Record<string, unknown>;
  // 功能：bandi:panel v1→v2——作用域键迁移（旧裸会话键加 session: 前缀）
  const migrated =
    version < 2
      ? {
          ...state,
          dismissedAuto: prefixScopeKeys(state.dismissedAuto),
          autoOpened: prefixScopeKeys(state.autoOpened),
          widthBySession: prefixScopeKeys(state.widthBySession),
        }
      : state;
  // 功能：bandi:panel v2→v3——宽度语义改「手动覆盖」（旧固定值作废，回窗口自适应）
  if (version < 3) return { ...migrated, widthBySession: {} };
  return migrated;
}

export const usePanelStore = create<PanelState>()(
  persist(
    (set, get) => ({
      open: false,
      fullscreen: false,
      view: "overview",
      tabs: [],
      activeTabId: "",
      fileFocusPath: "",
      expandRoleId: "",
      toolFocus: null,
      taskFocusId: "",
      dismissedAuto: {},
      autoOpened: {},
      widthBySession: {},
      setOpen: (open) =>
        set((state) => ({
          open,
          fullscreen: open ? state.fullscreen : false,
        })),
      collapseByUser: (sessionId) =>
        set((state) => ({
          open: false,
          fullscreen: false,
          dismissedAuto: { ...state.dismissedAuto, [sessionId]: true },
        })),
      setFullscreen: (fullscreen) => set({ fullscreen }),
      toggleFullscreen: () => set((state) => ({ fullscreen: !state.fullscreen })),
      setView: (view) => set({ view, activeTabId: "" }),
      openTab: (tab) =>
        set((state) => {
          const id = makeTabId(tab);
          const exists = state.tabs.some((item) => item.id === id);
          let tabs = exists ? state.tabs : [...state.tabs, { ...tab, id }];
          if (tabs.length > MAX_TABS) {
            const drop = tabs.find((item) => item.id !== state.activeTabId && item.id !== id);
            if (drop) tabs = tabs.filter((item) => item.id !== drop.id);
          }
          return { tabs, activeTabId: id };
        }),
      closeTab: (id) =>
        set((state) => {
          const index = state.tabs.findIndex((item) => item.id === id);
          const tabs = state.tabs.filter((item) => item.id !== id);
          if (state.activeTabId !== id) return { tabs };
          const next = tabs[Math.min(index, tabs.length - 1)];
          return { tabs, activeTabId: next?.id ?? "" };
        }),
      activateTab: (id) => set({ activeTabId: id }),
      focusAttachment: (uploadId, title) => {
        get().openTab({ kind: "attachment", title, uploadId });
        set({ open: true });
      },
      focusFile: (path, title, projectId) => {
        const name = title ?? path.split("/").pop() ?? path;
        get().openTab({ kind: "file", title: name, path, projectId });
        set({ open: true, view: "files", fileFocusPath: path });
      },
      focusArtifact: (artifactId, title, path) => {
        get().openTab({ kind: "artifact", title, artifactId, path });
        set({ open: true, fileFocusPath: path ?? "" });
      },
      focusEmployee: (roleId) => set({ open: true, view: "employees", expandRoleId: roleId }),
      focusTool: (messageId, nodeId) =>
        set({ open: true, view: "details", toolFocus: { messageId, nodeId } }),
      clearToolFocus: () => set({ toolFocus: null }),
      focusTask: (taskId) => set({ open: true, view: "overview", taskFocusId: taskId }),
      maybeAutoOpen: (sessionId) =>
        set((state) => {
          if (state.dismissedAuto[sessionId] || state.autoOpened[sessionId]) return state;
          return {
            open: true,
            autoOpened: { ...state.autoOpened, [sessionId]: true },
          };
        }),
      resetForSession: () =>
        set({
          tabs: [],
          activeTabId: "",
          fileFocusPath: "",
          expandRoleId: "",
          toolFocus: null,
          taskFocusId: "",
        }),
      setWidth: (sessionId, width) =>
        set((state) => ({
          widthBySession: {
            ...state.widthBySession,
            [sessionId]: Math.round(Math.min(PANEL_WIDTH_MAX, Math.max(PANEL_WIDTH_MIN, width))),
          },
        })),
      resetWidth: (sessionId) =>
        set((state) => {
          const next = { ...state.widthBySession };
          delete next[sessionId];
          return { widthBySession: next };
        }),
    }),
    {
      name: "bandi:panel",
      version: 3,
      migrate: (persisted, version) => migratePanelPersisted(persisted, version) as PanelState,
      partialize: (state) => ({
        open: state.open,
        view: state.view,
        dismissedAuto: state.dismissedAuto,
        autoOpened: state.autoOpened,
        widthBySession: state.widthBySession,
      }),
    },
  ),
);
