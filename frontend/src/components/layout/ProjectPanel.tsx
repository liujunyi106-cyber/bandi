/**
 * 功能：项目面板（点击项目在左栏右侧展开，通高置顶）。
 * 内容：秘书（头像+名称+本项目秘书会话列表）+ 团队群聊段头
 * （.3 删甲后退化为纯段头，团队群聊由下方项目会话列表承载）+ 员工会话
 * （每员工头像+名称+独立会话列表）。
 * 顶栏收缩按钮 `collapsed` 一键收起；员工单聊仅在面板中展示。
 * 第十四轮：① 三类头像行统一「＋新建会话」（`MessageSquarePlus`
 * 聊天气泡加号）+「更多操作 ⋯」；秘书行 ⋯ 不含「从列表中删除」，员工行 ⋯ = 移除员工会话；
 * ② 标题栏「历史记录」icon 删除——上部改为搜索 icon
 * 点击向左展开成搜索框，展开且未输入时内容区呈现「历史任务」列表（含归属分区字段）；
 * ③ 历史任务/搜索覆盖秘书会话、群聊会话、员工会话三类（员工会话原缺失）。
 */
import {
  Archive,
  FolderOpen,
  ListChecks,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Search,
  Trash2,
  Users,
  UsersRound,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { AssistantAvatar } from "@/components/chat/AssistantAvatar";
import { RenameSessionDialog } from "@/components/chat/RenameSessionDialog";
import { PanelToggleIcon } from "@/components/layout/PanelToggleIcon";
import { TeamAvatar } from "@/components/team/TeamAvatar";
import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { SelectTeamDialog } from "@/components/chat/SelectTeamDialog";
import { SessionActivityIcon } from "@/components/chat/SessionActivityIcon";
import { NewSessionIcon } from "@/components/icons/NewSessionIcon";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { BulkCheckbox } from "@/components/ui/bulk-checkbox";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/i18n";
import { aggregateSessionActivity, shouldClearOnOpen } from "@/lib/session-activity";
import { cn } from "@/lib/utils";
import { companyApi, type EmployeeSummary, type ProjectRecord } from "@/services/company";
import {
  groupSessionsByEmployee,
  isEmployeeDmSession,
  sessionsApi,
  type SessionSummary,
} from "@/services/sessions";
import { schedulerApi, type ScheduledTask } from "@/services/scheduler";
import { ApiError } from "@/services/api";
import { useChatStore } from "@/stores/chat-store";
import { useWindowWidth } from "@/hooks/use-window-width";
import { adaptiveProjectPanelWidth } from "@/lib/panel-width";
import { useUiStore, clampProjectPanelWidth } from "@/stores/ui-store";

/** 功能：面板列表初始显示上限（超出折叠为「查看更多」）。 */
const PANEL_LIMIT = 5;

/** 功能：会话列表行缩进——与名称字段对齐（头像 24px + 行首 8px + 间隔 6px）。 */
const NAME_ALIGN = "pl-[38px]";

/** 功能：行内小图标按钮样式（头像行右侧的「＋新建会话」/「更多操作」）。
 * no-focus-ring： ——浮层关闭后触发器残留外部描边（Radix 归还焦点
 * → focus-visible ring 显现），icon 功能按钮一律不画焦点环。 */
const ROW_ICON =
  "no-focus-ring text-chat-muted/60 hover:text-foreground flex size-5 shrink-0 items-center justify-center rounded-md";

function automationTime(value: string | null): string {
  if (!value) return "未运行";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 功能：面板条目（历史任务 / 搜索共用；kind = 归属分区字段）。 */
interface PanelEntry {
  key: string;
  kind: string;
  label: string;
  created_at: string;
  open: () => void;
}

interface ProjectPanelProps {
  collapsed?: boolean;
  onToggle?: () => void;
}

export function ProjectPanel(props: ProjectPanelProps) {
  const location = useLocation();
  const isChatRoute = location.pathname === "/" || location.pathname.startsWith("/chat");
  if (!isChatRoute) return null;
  return <ProjectPanelContent {...props} />;
}

function ProjectPanelContent({ collapsed = false, onToggle }: ProjectPanelProps) {
  const t = useT();
  const navigate = useNavigate();
  // 拍板：项目面板宽度可由右边缘拖拽调整（手动覆盖持久化 + min/max 钳制）
  // 拍板：无手动覆盖时随窗口自适应（1024→160 / 1920→224），双击手柄回自适应
  // 拍板：拖拽上限收到 224；旧 override 可能超出上限 → 读取时同样钳制
  const windowWidth = useWindowWidth();
  const widthOverride = useUiStore((state) => state.projectPanelWidthOverride);
  const setPanelWidth = useUiStore((state) => state.setProjectPanelWidth);
  const resetPanelWidth = useUiStore((state) => state.resetProjectPanelWidth);
  const panelWidth =
    widthOverride === null
      ? adaptiveProjectPanelWidth(windowWidth)
      : clampProjectPanelWidth(widthOverride);
  const dragRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const [searchParams] = useSearchParams();
  const activeProjectId = useChatStore((state) => state.activeProjectId);
  const projectId = searchParams.get("project") || activeProjectId || "";
  const activeView = searchParams.get("view");
  const sessions = useChatStore((state) => state.sessions);
  const sessionActivity = useChatStore((state) => state.sessionActivity);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const [project, setProject] = useState<ProjectRecord | null>(null);
  // 功能菜单（选择团队/选择员工）仅项目群聊会话显示——
  // 员工单聊与项目内秘书会话隐藏（这些场景不消费团队组建入口）
  const activeSession = useMemo(
    () => sessions.find((item) => item.id === activeSessionId) ?? null,
    [sessions, activeSessionId],
  );
  /** 功能：群聊条目选中态——当前会话属于本项目且非秘书（含本地新建占位会话）。 */
  const groupEntrySelected =
    !activeView &&
    activeSession !== null &&
    activeSession.project_id === projectId &&
    activeSession.origin !== "secretary" &&
    !isEmployeeDmSession(activeSession);
  const showFeatureMenu =
    activeSession?.origin !== "secretary" &&
    (activeSession === null || !isEmployeeDmSession(activeSession));
  /** 功能：群聊行显示名（单团队且模板库有 team_id→name 映射时跟随团队名；命名由团队组建落地）。 */
  const [teamName, setTeamName] = useState("");
  const groupEntryName =
    project?.team_ids?.length === 1 && teamName ? teamName : t("chat.panel.groupEntry");
  const [employees, setEmployees] = useState<EmployeeSummary[]>([]);
  const [automationTasks, setAutomationTasks] = useState<ScheduledTask[]>([]);
  const [teamPickerOpen, setTeamPickerOpen] = useState(false);
  const [secretaryAll, setSecretaryAll] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  // 第十一轮：项目面板会话条目补齐任务区同级能力（置顶/重命名/删除/批量）
  const [renamingSession, setRenamingSession] = useState<SessionSummary | null>(null);
  const [deletingSession, setDeletingSession] = useState<SessionSummary | null>(null);
  const pinnedIds = useChatStore((state) => state.pinnedSessionIds);
  const pinnedSet = useMemo(() => new Set(pinnedIds), [pinnedIds]);
  // 群聊会话支持批量操作（员工会话不做）
  const [bulk, setBulk] = useState(false);
  const [bulkSelected, setBulkSelected] = useState<string[]>([]);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
  // 功能：hover / 聚焦中的会话 id——更多按钮仅此时浮现
  const [hoveredId, setHoveredId] = useState("");

  // 无团队时面板仅显示秘书（群聊/员工块隐藏）
  const hasTeam = (project?.members.length ?? 0) > 0;

  /** 功能：团队群聊头像成员——项目成员中的在职员工（最多 4 个由 TeamAvatar 截断）。
   *  成员集以项目解析结果为准（employee_name 已随接口返回），员工列表只补头像元数据——
   *  员工接口未就绪/失败时不再把头像组退化成单图标。 */
  const groupMembers = useMemo(() => {
    const byRole = new Map(employees.map((employee) => [employee.role_id, employee]));
    return (project?.members ?? [])
      .filter((member) => member.employee_status === "active")
      .map((member) => {
        const employee = byRole.get(member.role_id);
        return {
          name: employee?.name || member.employee_name || member.role_name || member.role_id,
          metadata: employee?.metadata,
        };
      });
  }, [employees, project]);

  // 团队群聊会话（项目会话）——按 status 兜底过滤已归档（服务端默认列表只返 active）
  const projectSessions = useMemo(() => {
    return sessions.filter(
      (session) =>
        session.project_id === projectId &&
        session.status !== "archived" &&
        session.origin !== "automation" &&
        !automationTasks.some((task) => task.session_id === session.id),
    );
  }, [sessions, projectId, automationTasks]);

  // 第十一轮：置顶优先 + 时间倒序（面板与任务区同一置顶语义；
  // 原实现仅按时间排序 → 面板置顶后不生效）
  const sortSessions = useCallback(
    (list: SessionSummary[]) =>
      [...list].sort(
        (a, b) =>
          Number(pinnedSet.has(b.id)) - Number(pinnedSet.has(a.id)) ||
          (b.created_at || "").localeCompare(a.created_at || ""),
      ),
    [pinnedSet],
  );

  const projectChatSessions = useMemo(
    () =>
      sortSessions(
        projectSessions.filter(
          (session) => session.origin !== "secretary" && !isEmployeeDmSession(session),
        ),
      ),
    [projectSessions, sortSessions],
  );

  const projectEmployeeGroups = useMemo(
    () => groupSessionsByEmployee(projectSessions),
    [projectSessions],
  );
  const employeeByRole = useMemo(
    () => new Map(employees.map((employee) => [employee.role_id, employee])),
    [employees],
  );
  const projectAutomationTasks = useMemo(
    () => automationTasks.filter((task) => task.project_id === projectId),
    [automationTasks, projectId],
  );
  const employeeAutomationByRole = useMemo(() => {
    const grouped = new Map<string, ScheduledTask[]>();
    projectAutomationTasks
      .filter((task) => task.target_type === "employee" && task.target_id)
      .forEach((task) => {
        const current = grouped.get(task.target_id) ?? [];
        current.push(task);
        grouped.set(task.target_id, current);
      });
    return grouped;
  }, [projectAutomationTasks]);
  const secretaryAutomation = useMemo(
    () => projectAutomationTasks.filter((task) => task.target_type !== "employee"),
    [projectAutomationTasks],
  );
  const employeeRoleIds = useMemo(() => {
    const ids = new Set(projectEmployeeGroups.keys());
    employeeAutomationByRole.forEach((_tasks, roleId) => ids.add(roleId));
    return [...ids];
  }, [projectEmployeeGroups, employeeAutomationByRole]);

  const projectSecretarySessions = useMemo(
    () => sortSessions(projectSessions.filter((session) => session.origin === "secretary")),
    [projectSessions, sortSessions],
  );
  // 功能：助理条目不再常驻——新建会话空态选中项目后（或已有秘书会话）才显示
  const assistantEntryProjects = useUiStore((store) => store.assistantEntryProjects);
  const showSecretaryEntry =
    projectSecretarySessions.length > 0 ||
    secretaryAutomation.length > 0 ||
    assistantEntryProjects.includes(projectId);

  /** 功能：批量选择目标 = 秘书会话（群聊条目化后不成列，不参与批量；员工无独立列表）。 */
  const bulkTargets = useMemo(() => projectSecretarySessions, [projectSecretarySessions]);

  /** 功能：项目详情刷新（选择团队拉入成功后同步成员）。 */
  const refreshProject = useCallback(() => {
    if (!projectId) return;
    void companyApi
      .projects()
      .then((result) => {
        setProject(result.projects.find((item) => item.project_id === projectId) ?? null);
      })
      .catch(() => undefined);
  }, [projectId]);

  useEffect(() => {
    if (!projectId) {
      setProject(null);
      setEmployees([]);
      return;
    }
    let cancelled = false;
    const loadEmployees = () => {
      void companyApi
        .employees()
        .then((result) => {
          if (!cancelled) setEmployees(result.employees);
        })
        .catch(() => undefined);
    };
    refreshProject();
    loadEmployees();
    window.addEventListener("bandi:employees-changed", loadEmployees);
    window.addEventListener("focus", loadEmployees);
    window.addEventListener("focus", refreshProject);
    return () => {
      cancelled = true;
      window.removeEventListener("bandi:employees-changed", loadEmployees);
      window.removeEventListener("focus", loadEmployees);
      window.removeEventListener("focus", refreshProject);
    };
  }, [projectId, refreshProject]);

  useEffect(() => {
    if (!projectId) {
      setAutomationTasks([]);
      return;
    }
    let cancelled = false;
    const loadAutomation = () => {
      void schedulerApi
        .list()
        .then((result) => {
          if (!cancelled) setAutomationTasks(result.tasks);
        })
        .catch(() => undefined);
    };
    loadAutomation();
    window.addEventListener("focus", loadAutomation);
    window.addEventListener("bandi:automation-changed", loadAutomation);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", loadAutomation);
      window.removeEventListener("bandi:automation-changed", loadAutomation);
    };
  }, [projectId]);

  // 团队名解析（预留）：单团队时尝试从团队模板库的 team_id→name 映射取名——
  // 现无实例化记录（命名在「团队组建」落地），匹配不到即回落通用名「项目总群聊」
  useEffect(() => {
    const firstTeam = project?.team_ids?.[0] ?? "";
    if (!firstTeam) {
      setTeamName("");
      return;
    }
    let cancelled = false;
    void companyApi
      .teams()
      .then(({ teams }) => {
        if (cancelled) return;
        // 模板库有同名模板 → 用模板名；自定义实例（无模板）回落 team_id 本身作显示名
        setTeamName(teams.find((item) => item.team_id === firstTeam)?.name ?? firstTeam);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [project]);

  useEffect(() => {
    setQuery("");
    setSearchOpen(false);
    // 切项目重置批量态：防选中项指向旧项目会话被误删（-3 修复）
    setBulk(false);
    setBulkSelected([]);
    setBulkConfirmOpen(false);
  }, [projectId]);

  const openSession = useCallback(
    (session: SessionSummary) => {
      useChatStore.getState().setActiveSession(session.id);
      useChatStore.getState().setActiveProject(session.project_id || "");
      // 已读语义：done 圆点在用户打开该会话后清除（running 不清——在跑就得继续显示）
      const store = useChatStore.getState();
      if (shouldClearOnOpen(store.sessionActivity[session.id])) {
        store.clearSessionActivity(session.id);
      }
      // 面板内点会话保留项目上下文（面板不收缩）
      setQuery("");
      setSearchOpen(false);
      navigate(session.project_id ? `/?project=${session.project_id}` : "/");
    },
    [navigate],
  );

  const openAutomationTask = useCallback(
    (task: ScheduledTask) => {
      if (!task.session_id) {
        toast.info("这个任务还没有运行会话");
        return;
      }
      const store = useChatStore.getState();
      store.setActiveSession(task.session_id);
      store.setActiveProject(task.project_id || projectId);
      setQuery("");
      setSearchOpen(false);
      navigate(task.project_id ? `/?project=${task.project_id}` : "/");
    },
    [navigate, projectId],
  );

  const deleteAutomationTask = useCallback(async (task: ScheduledTask) => {
    if (!window.confirm(`删除「${task.name}」及其运行会话历史？`)) return;
    try {
      await schedulerApi.remove(task.task_id);
      setAutomationTasks((current) => current.filter((item) => item.task_id !== task.task_id));
      toast.success("已删除自动化任务");
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    }
  }, []);

  const automationRows = (tasks: ScheduledTask[]) =>
    tasks.map((task) => (
      <div
        key={task.task_id}
        className="group hover:bg-primary/5 relative flex w-full items-center gap-1 rounded-lg pr-1 pl-[38px]"
      >
        <button
          type="button"
          data-testid={`panel-automation-task-${task.task_id}`}
          className="text-chat-muted hover:text-foreground flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left"
          onClick={() => openAutomationTask(task)}
        >
          <span
            className={cn(
              "size-1.5 shrink-0 rounded-full",
              !task.enabled
                ? "bg-muted-foreground/40"
                : task.last_error
                  ? "bg-destructive"
                  : "bg-emerald-500",
            )}
          />
          <span className="min-w-0 flex-1 truncate text-xs">{task.name}</span>
        </button>
        <span className="text-chat-muted shrink-0 text-[9px]">
          {automationTime(task.last_run_at)}
        </span>
        <button
          type="button"
          data-testid={`panel-automation-delete-${task.task_id}`}
          className="text-chat-muted/60 hover:text-destructive flex size-4 shrink-0 items-center justify-center rounded opacity-0 transition-opacity group-hover:opacity-100"
          title="删除自动化任务"
          onClick={(event) => {
            event.stopPropagation();
            void deleteAutomationTask(task);
          }}
        >
          <X className="size-3" />
        </button>
      </div>
    ));

  const newEmployeeSession = useCallback(
    async (roleId: string, employeeName: string) => {
      try {
        const { session } = await sessionsApi.create({
          title: employeeName,
          project_id: projectId,
          origin: "employee_dm",
          target_role: roleId,
          employee_name: employeeName,
        });
        const store = useChatStore.getState();
        store.upsertSession({
          id: session.id,
          title: session.title || employeeName,
          created_at: session.created_at || new Date().toISOString(),
          message_count: 0,
          project_id: projectId,
          origin: "employee_dm",
          target_role: roleId,
          employee_name: employeeName,
        });
        store.setActiveSession(session.id);
        store.setActiveProject(projectId);
        navigate(`/?project=${projectId}`);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : String(error));
      }
    },
    [navigate, projectId],
  );

  /** 功能：会话组状态 = 该组下所有会话的聚合（排除当前会话，与单条会话行同口径）。 */
  const groupActivity = useCallback(
    (list: SessionSummary[]) =>
      aggregateSessionActivity(
        list.map((item) => item.id),
        sessionActivity,
        activeSessionId,
      ),
    [sessionActivity, activeSessionId],
  );

  const sessionLabel = (session: SessionSummary) =>
    session.title?.trim() || `${session.id.slice(0, 8)}…`;

  /** 功能：历史任务条目（**仅本项目**，归属 = 项目空间；按左栏「任务/项目空间」口径标注）。 */
  const panelEntries = useMemo(() => {
    if (!projectId) return [] as PanelEntry[];
    const label = (session: SessionSummary) =>
      session.title?.trim() || `${session.id.slice(0, 8)}…`;
    const list: PanelEntry[] = [];
    for (const session of projectSecretarySessions) {
      list.push({
        key: `secretary-${session.id}`,
        kind: t("chat.panel.secretaryEntry"),
        label: label(session),
        created_at: session.created_at || "",
        open: () => openSession(session),
      });
    }
    for (const session of projectChatSessions) {
      list.push({
        key: `group-${session.id}`,
        kind: t("chat.panel.groupEntry"),
        label: label(session),
        created_at: session.created_at || "",
        open: () => openSession(session),
      });
    }
    for (const [roleId, group] of projectEmployeeGroups) {
      const name = employeeByRole.get(roleId)?.name || group[0]?.employee_name || roleId;
      for (const session of group) {
        list.push({
          key: `employee-${roleId}-${session.id}`,
          kind: name,
          label: label(session),
          created_at: session.created_at || "",
          open: () => openSession(session),
        });
      }
    }
    return list.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }, [
    projectId,
    projectSecretarySessions,
    projectChatSessions,
    projectEmployeeGroups,
    employeeByRole,
    t,
    openSession,
  ]);

  /** 功能：历史/搜索结果按分区归组（子标题 = 秘书 / 项目总群聊 / 员工姓名）。 */
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const visible = q
      ? panelEntries.filter(
          (entry) => entry.label.toLowerCase().includes(q) || entry.kind.toLowerCase().includes(q),
        )
      : panelEntries;
    const baseKinds = [t("chat.panel.secretaryEntry"), t("chat.panel.groupEntry")];
    const employeeKinds = Array.from(
      new Set(visible.map((entry) => entry.kind).filter((kind) => !baseKinds.includes(kind))),
    );
    return [...baseKinds, ...employeeKinds]
      .map((kind) => ({ kind, entries: visible.filter((entry) => entry.kind === kind) }))
      .filter((group) => group.entries.length > 0);
  }, [panelEntries, query, t]);

  if (!projectId || collapsed) return null;

  /** 功能：新建会话——按行类型归属（秘书行 → 秘书会话 / 群聊行 → 团队会话）。 */
  const newSession = (origin: "" | "secretary") => {
    const store = useChatStore.getState();
    store.setActiveSession(null);
    store.setActiveProject(projectId);
    store.setPendingSessionOrigin(origin);
    navigate(`/?project=${projectId}`);
  };

  /** 功能：团队总群聊入口——条目即唯一入口（优先打开有消息的会话，空置占位不遮蔽真实群聊；
   * 无会话则后端建会话再打开）。 */
  const canonicalGroupSession =
    projectChatSessions.find((session) => (session.message_count ?? 0) > 0) ??
    projectChatSessions[0] ??
    null;
  const openGroupChat = () => {
    if (canonicalGroupSession) {
      openSession(canonicalGroupSession);
      return;
    }
    // 无群聊会话：后端先建空会话（落库）再打开——选中态不随列表刷新丢失
    void sessionsApi
      .create({ title: t("chat.panel.groupEntry"), project_id: projectId })
      .then(({ session }) => {
        const store = useChatStore.getState();
        store.upsertSession({
          id: session.id,
          title: session.title ?? "",
          created_at: session.created_at ?? new Date().toISOString(),
          message_count: 0,
          project_id: projectId,
        });
        store.setActiveSession(session.id);
        store.setActiveProject(projectId);
        setQuery("");
        setSearchOpen(false);
        navigate(`/?project=${projectId}`);
      })
      .catch((error) => {
        toast.error(error instanceof Error ? error.message : String(error));
      });
  };

  /** 功能：删除当前群聊会话（团队和项目保留；再拉团队生成新群聊）。 */
  const deleteGroupChat = async (session: SessionSummary) => {
    const store = useChatStore.getState();
    const activeId = store.activeSessionId;
    store.removeSessionLocal(session.id);
    if (activeId === session.id) store.setActiveSession(null);
    try {
      await sessionsApi.remove(session.id);
      toast.success(t("chat.session.deleted"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        toast.success(t("chat.session.deleted"));
        return;
      }
      toast.error(t("chat.session.deleteFailed"));
      void sessionsApi
        .list()
        .then((list) => useChatStore.getState().setSessions(list))
        .catch(() => undefined);
    }
  };

  /** 功能：拉入团队后确保群聊入口——无群聊会话时补建一个空群聊
   *  （删除群聊后经顶部「选择团队」重新拉入即可恢复入口行）。 */
  const ensureGroupSession = async () => {
    await refreshProject();
    // 以 store 现状判断（memo 可能滞后于本帧）——已有群聊会话则不再补建
    const hasGroup = useChatStore
      .getState()
      .sessions.some(
        (item) =>
          item.project_id === projectId &&
          item.origin !== "secretary" &&
          item.status !== "archived",
      );
    if (hasGroup) return;
    try {
      const { session } = await sessionsApi.create({
        title: t("chat.panel.groupEntry"),
        project_id: projectId,
      });
      useChatStore.getState().upsertSession({
        id: session.id,
        title: session.title ?? "",
        created_at: session.created_at ?? new Date().toISOString(),
        message_count: 0,
        project_id: projectId,
      });
    } catch {
      // 建会话失败不阻断拉团队成功（用户可经项目行打开时再建）
    }
  };

  /** 功能：删除员工单聊——本地立即清条目/过程槽，后台删后端直聊线程（窗口边界）；
   *  重新选择员工即新线程新上下文（团队记忆/产物不删）。 */
  const openFolder = () => {
    void companyApi
      .openFolder(projectId)
      .then(() => toast.success(t("chat.project.folderOpened")))
      .catch((error) => toast.error(error instanceof Error ? error.message : String(error)));
  };

  /** 功能：批量选择（仅群聊会话）——切换选中 / 全选 / 退出。 */
  const toggleBulkItem = (id: string) =>
    setBulkSelected((previous) =>
      previous.includes(id) ? previous.filter((item) => item !== id) : [...previous, id],
    );

  const exitBulk = () => {
    setBulk(false);
    setBulkSelected([]);
  };

  const bulkArchive = async () => {
    if (bulkSelected.length === 0) return;
    const results = await Promise.allSettled(bulkSelected.map((id) => sessionsApi.archive(id)));
    const failed = results.filter((item) => item.status === "rejected").length;
    if (failed > 0) {
      toast.error(t("chat.bulk.archiveFailed").replace("{n}", String(failed)));
    } else {
      toast.success(t("chat.bulk.archived").replace("{n}", String(bulkSelected.length)));
    }
    exitBulk();
  };

  const bulkDelete = async () => {
    if (bulkSelected.length === 0) return;
    setBulkBusy(true);
    const removed = new Set<string>();
    try {
      for (const id of bulkSelected) {
        try {
          await sessionsApi.remove(id);
        } catch (error) {
          // 404 = 后端已无该会话——仍计入已删并本地清理
          if (!(error instanceof ApiError && error.status === 404)) {
            throw error;
          }
        }
        useChatStore.getState().removeSessionLocal(id);
        removed.add(id);
      }
      // 功能：秘书会话清空后助理条目不再保留（条目按需出现）
      const secretaryIds = new Set(projectSecretarySessions.map((item) => item.id));
      if (secretaryIds.size > 0 && bulkSelected.every((id) => secretaryIds.has(id))) {
        const remaining = projectSecretarySessions.filter((item) => !removed.has(item.id));
        if (remaining.length === 0) {
          useUiStore.getState().hideAssistantEntry(projectId);
        }
      }
      toast.success(t("chat.bulk.deleted").replace("{n}", String(removed.size)));
      exitBulk();
    } catch (error) {
      // 中途失败：剔除已删项、保留未删项供重试（-3 修复——原实现残留已删 id
      // 致「已选 N」虚高与重试 404）
      if (removed.size > 0) {
        setBulkSelected((previous) => previous.filter((id) => !removed.has(id)));
      }
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBulkBusy(false);
    }
  };

  /** 功能：单条删除会话（秘书/群聊共用）——本地立即清理，再请求后端。
   *  群聊条目化：删除任一「团队群聊会话」= 清空整个群聊（单条目口径）。 */
  const onDeleteSession = async (session: SessionSummary) => {
    if (isEmployeeDmSession(session)) {
      setDeletingSession(null);
      useChatStore.getState().removeSessionLocal(session.id);
      try {
        await sessionsApi.remove(session.id);
        toast.success(t("chat.session.deleted"));
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          toast.success(t("chat.session.deleted"));
        } else {
          toast.error(t("chat.session.deleteFailed"));
          void sessionsApi
            .list()
            .then((list) => useChatStore.getState().setSessions(list))
            .catch(() => undefined);
        }
      }
      return;
    }
    if (session.origin !== "secretary") {
      setDeletingSession(null);
      await deleteGroupChat(session);
      return;
    }
    // 秘书会话：先本地移除（条目立即消失），失败再恢复服务端真源
    const store = useChatStore.getState();
    const remaining = projectSecretarySessions.filter((item) => item.id !== session.id);
    store.removeSessionLocal(session.id);
    if (remaining.length === 0) {
      useUiStore.getState().hideAssistantEntry(projectId);
    }
    try {
      await sessionsApi.remove(session.id);
      toast.success(t("chat.session.deleted"));
    } catch (error) {
      // 404 = 目标已达成；其余失败重新拉取服务端列表恢复
      if (error instanceof ApiError && error.status === 404) {
        toast.success(t("chat.session.deleted"));
      } else {
        toast.error(t("chat.session.deleteFailed"));
        void sessionsApi
          .list()
          .then((list) => useChatStore.getState().setSessions(list))
          .catch(() => undefined);
      }
    } finally {
      setDeletingSession(null);
    }
  };

  /** 功能：归档会话——项目会话沉淀摘要到项目 AGENTS.md；普通会话仅置状态。 */
  const archiveSession = async (session: SessionSummary) => {
    try {
      const result = await sessionsApi.archive(session.id);
      void sessionsApi
        .list()
        .then((list) => useChatStore.getState().setSessions(list))
        .catch(() => undefined);
      if (result.distilled) {
        toast.success(t("chat.toast.archivedToProject"));
      } else if (result.archived) {
        toast.success(t("chat.toast.archived"));
      } else {
        toast.info(t("chat.toast.archivedNoSummary").replace("{reason}", result.reason));
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    }
  };

  /**
   * 功能：会话 ⋯ 菜单项。
   * ① 秘书/群聊**头像行**的 ⋯ 是**分组级**菜单——不含会话级项（置顶/重命名/删除）
   * 会话行才展示会话级操作；**灰色不可用项一律不渲染**（不摆"点不动"的项）。
   * ② 会话行与头像行共用本函数，保证两处渲染口径一致。
   */
  const sessionMenuItems = (
    session: SessionSummary | null,
    onBulk: () => void,
    withSessionOps = Boolean(session),
  ) => (
    <>
      {folderItem}
      <DropdownMenuSeparator />
      {withSessionOps && session ? (
        <DropdownMenuItem
          data-testid="panel-session-pin"
          onClick={() => useChatStore.getState().togglePin(session.id)}
        >
          {pinnedSet.has(session.id) ? <PinOff /> : <Pin />}
          {pinnedSet.has(session.id) ? t("chat.session.unpin") : t("chat.session.pin")}
        </DropdownMenuItem>
      ) : null}
      {withSessionOps && session ? (
        <DropdownMenuItem
          data-testid="panel-session-archive"
          onClick={() => void archiveSession(session)}
        >
          <Archive /> {t("chat.session.archive")}
        </DropdownMenuItem>
      ) : null}
      {withSessionOps && session ? (
        <DropdownMenuItem
          data-testid="panel-session-rename"
          onClick={() => setRenamingSession(session)}
        >
          <Pencil /> {t("chat.session.rename")}
        </DropdownMenuItem>
      ) : null}
      {withSessionOps && session ? <DropdownMenuSeparator /> : null}
      <DropdownMenuItem data-testid="panel-session-bulk" onClick={onBulk}>
        <ListChecks /> {t("chat.bulk.enter")}
      </DropdownMenuItem>
      {withSessionOps && session ? (
        <DropdownMenuItem
          data-testid="panel-session-delete"
          variant="destructive"
          onClick={() => setDeletingSession(session)}
        >
          <Trash2 /> {t("chat.session.delete")}
        </DropdownMenuItem>
      ) : null}
    </>
  );

  /** 功能：会话行菜单触发器（秘书/群聊会话行同一份）——仅 hover / 聚焦时渲染。 */
  const sessionMenu = (session: SessionSummary, onBulk: () => void) => (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          data-testid="panel-session-more"
          className={ROW_ICON}
          style={{
            opacity: hoveredId === session.id ? 1 : 0,
            pointerEvents: hoveredId === session.id ? "auto" : "none",
            transition: "opacity 120ms ease-out",
          }}
          onFocus={() => setHoveredId(session.id)}
          onBlur={() => setHoveredId((prev) => (prev === session.id ? "" : prev))}
          title={t("common.more")}
        >
          <MoreHorizontal className="size-3.5" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">{sessionMenuItems(session, onBulk)}</DropdownMenuContent>
    </DropdownMenu>
  );

  /** 功能：项目面板会话行（秘书/群聊共用）：批量态 = 原生 checkbox；常态 = 打开 + ⋯ 菜单。 */
  const panelSessionRow = (session: SessionSummary, scope: "secretary" | "group") => {
    if (bulk) {
      return (
        <label
          key={session.id}
          data-testid={scope === "group" ? "panel-bulk-session" : "panel-bulk-secretary"}
          className="hover:bg-primary/5 flex w-full cursor-pointer items-center gap-2 rounded-lg py-1 pr-2 pl-2 text-left"
        >
          <BulkCheckbox
            checked={bulkSelected.includes(session.id)}
            onChange={() => toggleBulkItem(session.id)}
            testId={scope === "group" ? "panel-bulk-session-box" : "panel-bulk-secretary-box"}
          />
          <span className="min-w-0 flex-1 truncate text-xs">{sessionLabel(session)}</span>
        </label>
      );
    }
    return (
      <div
        key={session.id}
        className={cn(
          "group text-muted-foreground hover:bg-primary/5 hover:text-foreground relative flex w-full items-center gap-1 rounded-lg pr-1",
          activeSessionId === session.id && !activeView && "bg-primary/5 text-foreground",
        )}
        onMouseEnter={() => setHoveredId(session.id)}
        onMouseLeave={() => setHoveredId((prev) => (prev === session.id ? "" : prev))}
      >
        <button
          type="button"
          data-testid={scope === "group" ? "panel-project-session" : "panel-secretary-session"}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left"
          onClick={() => openSession(session)}
        >
          {pinnedSet.has(session.id) ? <Pin className="text-primary size-3 shrink-0" /> : null}
          <span className={cn("min-w-0 flex-1 truncate text-xs", NAME_ALIGN)}>
            {sessionLabel(session)}
          </span>
        </button>
        {/* 右侧槽位：非 hover 显示活动圆点；hover 时更多按钮顶替该位置，圆点退到其左侧 */}
        <span
          data-testid={`panel-activity-slot-${session.id}`}
          aria-hidden
          className="pointer-events-none absolute top-1/2 -translate-y-1/2"
          style={{ right: hoveredId === session.id ? 30 : 8, transition: "right 120ms ease-out" }}
        >
          <SessionActivityIcon
            state={
              activeSessionId === session.id
                ? ""
                : aggregateSessionActivity([session.id], sessionActivity)
            }
            testId={`panel-activity-${session.id}`}
          />
        </span>
        {sessionMenu(session, () => {
          setBulk(true);
          setBulkSelected([session.id]);
        })}
      </div>
    );
  };

  const renderMore = (total: number, expanded: boolean, onToggle: () => void, testId: string) =>
    total > PANEL_LIMIT ? (
      <button
        type="button"
        data-testid={testId}
        className={cn(
          "text-chat-muted hover:text-foreground w-full py-1 pr-2 text-left text-[11px]",
          NAME_ALIGN,
        )}
        onClick={onToggle}
      >
        {expanded
          ? t("chat.showLess")
          : t("chat.showMore").replace("{n}", String(total - PANEL_LIMIT))}
      </button>
    ) : null;

  /** 功能：头像行内置「＋新建会话」（聊天气泡加号）——与「更多」同口径：
    * 平时隐形，条目悬浮 / 聚焦时浮现。 */
  const newSessionButton = (scope: string, onNew: () => void) => {
    const hoverKey = `entry:${scope}`;
    return (
      <button
        type="button"
        data-testid={`panel-${scope}-new-session`}
        className={ROW_ICON}
        title={t("chat.panel.newSession")}
        style={{
          opacity: hoveredId === hoverKey ? 1 : 0,
          pointerEvents: hoveredId === hoverKey ? "auto" : "none",
          transition: "opacity 120ms ease-out",
        }}
        onFocus={() => setHoveredId(hoverKey)}
        onBlur={() => setHoveredId((prev) => (prev === hoverKey ? "" : prev))}
        onClick={onNew}
      >
        {/* 2026-09-26 设计统一：与左栏「新建会话」共用自绘图标（同一语义同一图形） */}
        <NewSessionIcon className="size-3.5" />
      </button>
    );
  };

  /** 功能：头像行内置「更多操作 ⋯」（三类行统一入口，菜单内容按行类型区分）。
       * 与会话行同口径——平时隐形，条目悬浮 / 聚焦时浮现。 */
  const moreMenu = (scope: string, items: React.ReactNode) => {
    const hoverKey = `entry:${scope}`;
    return (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid={`panel-${scope}-more`}
            className={ROW_ICON}
            title={t("common.more")}
            style={{
              opacity: hoveredId === hoverKey ? 1 : 0,
              pointerEvents: hoveredId === hoverKey ? "auto" : "none",
              transition: "opacity 120ms ease-out",
            }}
            onFocus={() => setHoveredId(hoverKey)}
            onBlur={() => setHoveredId((prev) => (prev === hoverKey ? "" : prev))}
          >
            <MoreHorizontal className="size-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">{items}</DropdownMenuContent>
      </DropdownMenu>
    );
  };

  const folderItem = (
    <DropdownMenuItem key="folder" onClick={openFolder}>
      <FolderOpen /> {t("chat.project.openFolder")}
    </DropdownMenuItem>
  );

  return (
    // [&_svg]:stroke-[1.75]：与左栏导航 / 过程块统一描边（lucide 默认 2 在 12–14px 下偏重）
    <aside
      style={{ width: panelWidth }}
      className="border-chat-hairline bg-card relative flex shrink-0 flex-col border-r [&_svg]:stroke-[1.75]"
      data-testid="project-panel"
    >
      {/* 右边缘拖拽手柄（2026-10-03）：向右拖增宽，store 层钳制 [160, 224]；双击重置默认。
          right-0：8px 热区全在面板内（-right-1 伸出 4px 会污染 scrollWidth / 覆盖对话区边缘）。 */}
      <div
        data-testid="project-panel-resize-handle"
        role="separator"
        aria-orientation="vertical"
        className="hover:bg-primary/40 absolute right-0 top-0 z-10 h-full w-2 cursor-col-resize"
        onPointerDown={(event) => {
          dragRef.current = { startX: event.clientX, startWidth: panelWidth };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={(event) => {
          const drag = dragRef.current;
          if (!drag) return;
          setPanelWidth(drag.startWidth + (event.clientX - drag.startX));
        }}
        onPointerUp={() => {
          dragRef.current = null;
        }}
        onDoubleClick={() => resetPanelWidth()}
      />
      <div className="border-chat-hairline flex h-14 shrink-0 items-center gap-1 border-b px-3">
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {project?.name || projectId}
        </span>
        {/* 2026-09-23 用户拍板：保留原「功能」icon（选择团队/选择员工；无团队灰色禁用）
            2026-10-02 用户拍板：仅项目群聊会话显示（DM / 项目内秘书会话隐藏） */}
        {showFeatureMenu ? (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                data-testid="panel-feature-menu"
                className={cn(
                  "flex size-6 shrink-0 items-center justify-center rounded-md",
                  hasTeam ? "text-chat-muted hover:text-foreground" : "text-chat-muted/40",
                )}
                title={t("chat.panel.features")}
              >
                <UsersRound className="size-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                data-testid="panel-select-team"
                onClick={() => setTeamPickerOpen(true)}
              >
                <Users /> {t("chat.panel.selectTeam")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
        {/* 功能：项目面板收缩按钮——与「选择团队」同排、位于其右侧 */}
        <button
          type="button"
          data-testid="project-panel-collapse"
          className="text-chat-muted hover:bg-primary/5 hover:text-foreground flex size-8 shrink-0 items-center justify-center rounded-[8px]"
          title={t("chat.panel.collapse")}
          aria-label={t("chat.panel.collapse")}
          onClick={() => onToggle?.()}
        >
          <PanelToggleIcon side="left" />
        </button>
      </div>

      {/* 2026-09-23 用户拍板：搜索框常驻（与下方不设分割线）；聚焦即呈现历史任务 */}
      <div className="shrink-0 px-2 pt-2 pb-3">
        <div className="border-chat-hairline bg-card flex items-center gap-1.5 rounded-md border px-2 py-1.5">
          <Search className="text-chat-muted size-3.5 shrink-0" />
          <input
            data-testid="panel-search-input"
            className="text-chat-ink placeholder:text-chat-muted/60 min-w-0 flex-1 bg-transparent text-xs outline-none"
            placeholder={t("chat.panel.searchPlaceholder")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onFocus={() => setSearchOpen(true)}
          />
          {searchOpen ? (
            <button
              type="button"
              data-testid="panel-search-close"
              className="text-chat-muted hover:text-foreground flex size-4 shrink-0 items-center justify-center"
              title={t("common.cancel")}
              onClick={() => {
                setQuery("");
                setSearchOpen(false);
              }}
            >
              <X className="size-3" />
            </button>
          ) : null}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-1 pb-2">
        {searchOpen ? (
          <>
            <p className="text-chat-muted px-2 py-1 text-[11px]">
              {query.trim() ? t("chat.panel.searchResult") : t("chat.panel.history")}
            </p>
            {groups.length === 0 ? (
              <p className="text-chat-muted px-2 py-1 text-[11px]">
                {query.trim() ? t("chat.panel.searchEmpty") : t("chat.panel.historyEmpty")}
              </p>
            ) : (
              groups.map((group) => (
                <div key={group.kind} className="mb-1">
                  {/* 子标题（分区）：颜色更浅以区分下方会话记录 */}
                  <p
                    data-testid="panel-group-title"
                    className="text-chat-muted/60 px-2 py-1 text-[11px] font-medium"
                  >
                    {group.kind}
                  </p>
                  {group.entries.map((entry) => (
                    <button
                      key={entry.key}
                      type="button"
                      data-testid={query.trim() ? "panel-search-hit" : "panel-history-entry"}
                      className="hover:bg-primary/5 flex w-full items-center rounded-lg px-2 py-1 text-left text-foreground"
                      onClick={entry.open}
                    >
                      <span className="min-w-0 flex-1 truncate text-xs">{entry.label}</span>
                    </button>
                  ))}
                </div>
              ))
            )}
          </>
        ) : (
          <>
            {showSecretaryEntry ? (
              <>
                {/* 秘书（纯展示——2026-09-23 用户拍板：带头像组件不可点，仅会话行可点选中）
                2026-09-29 用户拍板：右侧按钮位置与下方会话行 ⋯ **上下对齐**——
                右侧内边距用 pr-1（会话行同值），不用 px-2（否则 ⋯ 右移 4px 错位）。 */}
                <div
                  data-testid="panel-secretary-entry"
                  className="text-muted-foreground flex w-full items-center gap-1.5 rounded-lg py-1 pl-2 pr-1 text-left"
                  onMouseEnter={() => setHoveredId("entry:secretary")}
                  onMouseLeave={() =>
                    setHoveredId((prev) => (prev === "entry:secretary" ? "" : prev))
                  }
                >
                  <AssistantAvatar className="size-6" />
                  <span className="min-w-0 flex-1 truncate text-xs">
                    {t("chat.panel.secretaryEntry")}
                  </span>
                  <SessionActivityIcon
                    state={groupActivity(projectSecretarySessions)}
                    testId="panel-secretary-activity"
                  />
                  {newSessionButton("secretary", () => newSession("secretary"))}
                  {moreMenu(
                    "secretary",
                    sessionMenuItems(null, () => setBulk(true), false),
                  )}
                </div>
                {(secretaryAll
                  ? projectSecretarySessions
                  : projectSecretarySessions.slice(0, PANEL_LIMIT)
                ).map((session) => panelSessionRow(session, "secretary"))}
                {renderMore(
                  projectSecretarySessions.length,
                  secretaryAll,
                  () => setSecretaryAll((value) => !value),
                  "secretary-more",
                )}
                {secretaryAutomation.length > 0 ? (
                  <div className="mt-1">
                    <p className="text-chat-muted/60 py-1 pl-[38px] text-[10px]">
                      自动化<span className="ml-1">{secretaryAutomation.length}</span>
                    </p>
                    {automationRows(secretaryAutomation)}
                  </div>
                ) : null}
              </>
            ) : null}

            {/* 群聊入口行：hasTeam 且存在群聊会话时显示——删除群聊（清空会话）后整行消失，
                经顶部「选择团队」重新拉入（幂等）自动补建空群聊恢复入口。 */}
            {projectChatSessions.length > 0 ? (
              <div
                data-testid="panel-group-entry"
                className={cn(
                  "text-muted-foreground mt-1.5 flex w-full items-center gap-1 rounded-lg pr-1",
                  groupEntrySelected && "bg-primary/5 text-foreground",
                )}
                onMouseEnter={() => setHoveredId("entry:group")}
                onMouseLeave={() => setHoveredId((prev) => (prev === "entry:group" ? "" : prev))}
              >
                {/* 团队总群聊：条目即入口（点击打开群聊，无会话则新建）；右侧直接删除 */}
                <button
                  type="button"
                  data-testid="panel-group-open"
                  className="flex min-w-0 flex-1 items-center gap-1.5 px-2 py-1 text-left"
                  onClick={openGroupChat}
                >
                  <TeamAvatar members={groupMembers} className="size-6" />
                  <span className="min-w-0 flex-1 truncate text-xs">{groupEntryName}</span>
                </button>
                <SessionActivityIcon
                  state={groupActivity(projectChatSessions)}
                  testId="panel-group-activity"
                />
                {/* 功能：归档群聊（悬浮浮现）——归档后从面板隐藏，历史沉淀到项目 AGENTS.md */}
                <button
                  type="button"
                  data-testid="panel-group-archive"
                  className={ROW_ICON}
                  title={t("chat.session.archive")}
                  style={{
                    opacity: hoveredId === "entry:group" ? 1 : 0,
                    pointerEvents: hoveredId === "entry:group" ? "auto" : "none",
                    transition: "opacity 120ms ease-out",
                  }}
                  onFocus={() => setHoveredId("entry:group")}
                  onBlur={() => setHoveredId((prev) => (prev === "entry:group" ? "" : prev))}
                  onClick={() => {
                    if (canonicalGroupSession) {
                      void archiveSession(canonicalGroupSession);
                    } else {
                      toast.info(t("chat.panel.sessionsEmpty"));
                    }
                  }}
                >
                  <Archive className="size-3.5" />
                </button>
                <button
                  type="button"
                  data-testid="panel-group-delete"
                  className={ROW_ICON}
                  title={t("chat.session.delete")}
                  style={{
                    opacity: hoveredId === "entry:group" ? 1 : 0,
                    pointerEvents: hoveredId === "entry:group" ? "auto" : "none",
                    transition: "opacity 120ms ease-out",
                  }}
                  onFocus={() => setHoveredId("entry:group")}
                  onBlur={() => setHoveredId((prev) => (prev === "entry:group" ? "" : prev))}
                  onClick={() => {
                    // 删除团队总群聊（经确认弹窗；无会话可删时提示）
                    if (canonicalGroupSession) {
                      setDeletingSession(canonicalGroupSession);
                    } else {
                      toast.info(t("chat.panel.sessionsEmpty"));
                    }
                  }}
                >
                  <X className="size-3.5" />
                </button>
              </div>
            ) : null}

            {employeeRoleIds.map((roleId) => {
              const group = projectEmployeeGroups.get(roleId) ?? [];
              const automation = employeeAutomationByRole.get(roleId) ?? [];
              const employee = employeeByRole.get(roleId);
              const name = employee?.name || group[0]?.employee_name || roleId;
              return (
                <div key={`employee-${roleId}`} className="mt-1.5">
                  <div
                    data-testid={`panel-employee-entry-${roleId}`}
                    className="text-muted-foreground flex w-full items-center gap-1.5 rounded-lg py-1 pr-1 pl-2"
                  >
                    <EmployeeAvatar
                      name={name}
                      metadata={employee?.metadata}
                      className="size-6 rounded-full text-[10px]"
                    />
                    <button
                      type="button"
                      className="min-w-0 flex-1 truncate text-left text-xs"
                      onClick={() =>
                        group[0] ? openSession(group[0]) : openAutomationTask(automation[0])
                      }
                    >
                      {name}
                    </button>
                    {group.length > 0 ? (
                      <SessionActivityIcon
                        state={groupActivity(group)}
                        testId={`panel-employee-activity-${roleId}`}
                      />
                    ) : null}
                    <button
                      type="button"
                      data-testid={`panel-employee-new-${roleId}`}
                      className={ROW_ICON}
                      title={t("nav.newTask")}
                      onClick={() => void newEmployeeSession(roleId, name)}
                    >
                      <NewSessionIcon className="size-3.5" />
                    </button>
                  </div>
                  {group.slice(0, PANEL_LIMIT).map((session) => panelSessionRow(session, "group"))}
                  {automation.length > 0 ? (
                    <div className="mt-1">
                      <p className="text-chat-muted/60 py-1 pl-[38px] text-[10px]">
                        自动化<span className="ml-1">{automation.length}</span>
                      </p>
                      {automationRows(automation)}
                    </div>
                  ) : null}
                </div>
              );
            })}
          </>
        )}
      </div>

      {/* 2026-09-25 用户拍板：与任务面板批量操作同款（面板内分区 + 按钮组，非弹窗） */}
      {bulk ? (
        <div
          data-testid="panel-bulk-bar"
          className="border-primary/5 bg-card shrink-0 border-t px-2 py-2"
        >
          <div className="flex items-center gap-2">
            <label className="flex cursor-pointer items-center gap-1.5 text-[11px]">
              <BulkCheckbox
                checked={bulkTargets.length > 0 && bulkSelected.length === bulkTargets.length}
                onChange={() =>
                  setBulkSelected(
                    bulkSelected.length === bulkTargets.length
                      ? []
                      : bulkTargets.map((session) => session.id),
                  )
                }
                testId="panel-bulk-all"
              />
              {t("chat.bulk.selectAll")}
            </label>
            <span className="text-muted-foreground flex-1 truncate text-[11px]">
              {t("chat.bulk.selected").replace("{n}", String(bulkSelected.length))}
            </span>
            <Button
              variant="ghost"
              size="icon-xs"
              title={t("common.cancel")}
              data-testid="panel-bulk-exit"
              onClick={exitBulk}
            >
              <X className="size-3.5" />
            </Button>
          </div>
          <div className="flex gap-2 pt-2">
            <Button
              variant="destructive"
              className="border-primary/5 h-8 flex-1 gap-1.5 rounded-xl"
              data-testid="panel-bulk-delete"
              disabled={bulkSelected.length === 0 || bulkBusy}
              onClick={() => setBulkConfirmOpen(true)}
            >
              <Trash2 className="size-3.5" />
              {t("chat.bulk.delete")}
            </Button>
            <Button
              variant="secondary"
              className="border-primary/5 h-8 flex-1 gap-1.5 rounded-xl"
              data-testid="panel-bulk-archive"
              disabled={bulkSelected.length === 0}
              onClick={bulkArchive}
            >
              <Archive className="size-3.5" />
              {t("chat.bulk.archive")}
            </Button>
          </div>
        </div>
      ) : null}

      <RenameSessionDialog
        session={renamingSession}
        onOpenChange={(open) => !open && setRenamingSession(null)}
      />

      <AlertDialog
        open={deletingSession !== null}
        onOpenChange={(open) => !open && setDeletingSession(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("chat.session.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("chat.session.deleteHint")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              data-testid="panel-session-delete-confirm"
              onClick={() => {
                if (deletingSession) void onDeleteSession(deletingSession);
              }}
            >
              {t("chat.session.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <SelectTeamDialog
        projectId={projectId}
        open={teamPickerOpen}
        onOpenChange={setTeamPickerOpen}
        onAdded={() => void ensureGroupSession()}
      />

      <AlertDialog open={bulkConfirmOpen} onOpenChange={setBulkConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("chat.bulk.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("chat.bulk.deleteHint").replace("{n}", String(bulkSelected.length))}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              data-testid="panel-bulk-delete-confirm"
              onClick={() => void bulkDelete()}
            >
              {t("chat.bulk.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </aside>
  );
}
