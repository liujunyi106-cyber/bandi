/**
 * 功能：左栏上下文列表区。
 * 两段（取消 Tab）：
 * - 「任务」：无项目归属的普通会话 + 无主项目会话兜底（项目被删后回归）；
 * - 「项目空间」：项目行 = 项目会话入口（点击打开最新会话；无会话进新建态）；
 * 「⋯」→ 用工会话 / 打开文件夹 / 从列表中删除。
 * 区标题：折叠箭头 + 计数（无图标）；列表超 5 条显示「查看更多（N）/收起」。
 * 项目行：[文件夹图标] 名称 · 成员数 [新建会话 icon] [⋯]；会话行无常规图标（置顶 pin 除外）。
 */
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  ListChecks,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";

import { RenameProjectDialog } from "@/components/layout/RenameProjectDialog";
import { RenameSessionDialog } from "@/components/chat/RenameSessionDialog";
import { SessionActivityIcon } from "@/components/chat/SessionActivityIcon";
import { NewSessionIcon } from "@/components/icons/NewSessionIcon";
import { AssistantAvatar } from "@/components/chat/AssistantAvatar";
import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { useEmployees } from "@/hooks/use-employees";
import { schedulerApi, type ScheduledTask } from "@/services/scheduler";
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
import { ApiError } from "@/services/api";
import { companyApi, type ProjectRecord } from "@/services/company";
import {
  groupSessionsByEmployee,
  groupSessionsByProject,
  isEmployeeDmSession,
  sessionsApi,
  type SessionSummary,
} from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

// 选中态 = 悬停态（同底色黑字），弃用描边方案
const ACTIVE_CLASS = "bg-primary/5 text-foreground";

/** 功能：区列表初始显示上限（超出折叠为「查看更多」）。 */
const SECTION_LIMIT = 5;

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

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

interface SectionHeaderProps {
  label: string;
  count: number;
  open: boolean;
  onToggle: () => void;
  testId: string;
}

/** 功能：区标题（名称+计数 + 右侧紧贴的折叠箭头，无图标）。 */
function SectionHeader({ label, count, open, onToggle, testId }: SectionHeaderProps) {
  return (
    <button
      type="button"
      data-testid={testId}
      // v5：上下文区段标题（任务/项目空间）相对导航项放大（13px）
      className="text-chat-muted hover:text-foreground flex w-full items-center gap-0.5 px-1 py-1 text-[13px]"
      onClick={onToggle}
    >
      <span className="truncate text-left">
        {label}（{count}）
      </span>
      {open ? (
        <ChevronDown className="size-3.5 shrink-0" />
      ) : (
        <ChevronRight className="size-3.5 shrink-0" />
      )}
    </button>
  );
}

export function SidebarContextList() {
  const t = useT();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const onChatRoute = location.pathname === "/";
  const sessions = useChatStore((state) => state.sessions);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const pinnedIds = useChatStore((state) => state.pinnedSessionIds);
  const sessionActivity = useChatStore((state) => state.sessionActivity);
  const dismissedProjectSessions = useChatStore((state) => state.dismissedProjectSessions);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [automationTasks, setAutomationTasks] = useState<ScheduledTask[]>([]);
  const [automationOpen, setAutomationOpen] = useState(true);
  const [renaming, setRenaming] = useState<SessionSummary | null>(null);
  const [deleting, setDeleting] = useState<SessionSummary | null>(null);
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  // 功能：hover / 聚焦中的会话 id——更多按钮仅此时浮现（平时圆点占其位置）
  const [hoveredId, setHoveredId] = useState("");
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);

  const [removingProject, setRemovingProject] = useState<ProjectRecord | null>(null);
  const [renamingProject, setRenamingProject] = useState<ProjectRecord | null>(null);
  // 修复：段落折叠 / 「查看更多」展开态外置到 ui-store——
  // 左栏 offcanvas 折叠会卸载本组件（AppSidebar showContextList），组件内 useState 重挂载即重置
  const tasksOpen = useUiStore((state) => state.sidebarTasksOpen);
  const projectsOpen = useUiStore((state) => state.sidebarProjectsOpen);
  const tasksAll = useUiStore((state) => state.sidebarTasksAll);
  const projectsAll = useUiStore((state) => state.sidebarProjectsAll);
  const toggleTasksOpen = useUiStore((state) => state.toggleSidebarTasksOpen);
  const toggleProjectsOpen = useUiStore((state) => state.toggleSidebarProjectsOpen);
  const toggleTasksAll = useUiStore((state) => state.toggleSidebarTasksAll);
  const toggleProjectsAll = useUiStore((state) => state.toggleSidebarProjectsAll);
  // 项目空间卡片新建项目后需要左栏重取（本组件挂载时只取一次）
  const projectsVersion = useUiStore((state) => state.projectsVersion);

  const refreshSessions = useCallback(
    () =>
      sessionsApi
        .list()
        .then((list) => useChatStore.getState().setSessions(list))
        .catch(() => undefined),
    [],
  );

  useEffect(() => {
    void refreshSessions();
    void companyApi
      .projects()
      .then((result) => setProjects(result.projects))
      .catch(() => undefined);
    const loadAutomation = () => {
      void schedulerApi
        .list()
        .then((result) => setAutomationTasks(result.tasks))
        .catch(() => undefined);
    };
    loadAutomation();
    window.addEventListener("focus", loadAutomation);
    window.addEventListener("bandi:automation-changed", loadAutomation);
    return () => {
      window.removeEventListener("focus", loadAutomation);
      window.removeEventListener("bandi:automation-changed", loadAutomation);
    };
  }, [projectsVersion, refreshSessions]);

  // 批量选择激活时隐藏侧栏底部按钮（通知/设置），批量分区贴住底部
  useEffect(() => {
    useUiStore.getState().setBulkSelectionActive(selectionMode);
    return () => useUiStore.getState().setBulkSelectionActive(false);
  }, [selectionMode]);

  const pinnedSet = useMemo(() => new Set(pinnedIds), [pinnedIds]);
  // 已移出项目列表的项目，其旧会话不再显示（本地隐藏位；后端记录与磁盘文件不动）
  const dismissedSessionIds = useMemo(
    () => new Set(Object.values(dismissedProjectSessions).flat()),
    [dismissedProjectSessions],
  );
  const sortPinnedFirst = useCallback(
    (list: SessionSummary[]) =>
      [...list].sort((a, b) => Number(pinnedSet.has(b.id)) - Number(pinnedSet.has(a.id))),
    [pinnedSet],
  );
  // 归档隐藏由服务端 status 决定（默认列表只返 active），此处按 status 兜底过滤
  const visibleSessions = useMemo(
    () =>
      sortPinnedFirst(
        sessions.filter(
          (session) => session.status !== "archived" && !dismissedSessionIds.has(session.id),
        ),
      ),
    [sessions, sortPinnedFirst, dismissedSessionIds],
  );
  const grouped = useMemo(() => groupSessionsByProject(visibleSessions), [visibleSessions]);
  const knownProjectIds = useMemo(
    () => new Set(projects.map((project) => project.project_id)),
    [projects],
  );
  // 任务区 = 普通会话（无项目 + 无主项目会话兜底；无主=项目被删后回归任务区，数据不丢可见）
  // 用户二次带项目归属的会话不进任务区（在项目空间/项目面板查看）
  const taskSessions = useMemo(
    () =>
      [
        ...(grouped.get("") ?? []),
        ...visibleSessions.filter(
          (session) => session.project_id && !knownProjectIds.has(session.project_id),
        ),
      ].filter(
        (session) =>
          session.origin !== "automation" &&
          !automationTasks.some((task) => task.session_id === session.id),
      ),
    [grouped, visibleSessions, knownProjectIds, automationTasks],
  );
  const { employees } = useEmployees();
  const employeeByRole = useMemo(
    () => new Map(employees.map((employee) => [employee.role_id, employee])),
    [employees],
  );
  const automationGroups = useMemo(() => {
    const groups = new Map<
      string,
      { key: string; name: string; employee?: (typeof employees)[number]; tasks: ScheduledTask[] }
    >();
    automationTasks
      .filter((task) => !task.project_id)
      .forEach((task) => {
        const isEmployee = task.target_type === "employee";
        const key = isEmployee ? task.target_id || "employee" : "assistant";
        const employee = isEmployee ? employeeByRole.get(task.target_id) : undefined;
        const group = groups.get(key) ?? {
          key,
          name: isEmployee ? employee?.name || task.target_id || "员工" : "助理",
          employee,
          tasks: [],
        };
        group.tasks.push(task);
        groups.set(key, group);
      });
    return [...groups.values()].sort((a, b) => {
      if (a.key === "assistant") return 1;
      if (b.key === "assistant") return -1;
      return a.name.localeCompare(b.name);
    });
  }, [automationTasks, employeeByRole, employees]);
  const taskPlainSessions = useMemo(
    () => taskSessions.filter((session) => !isEmployeeDmSession(session)),
    [taskSessions],
  );
  const taskEmployeeGroups = useMemo(() => groupSessionsByEmployee(taskSessions), [taskSessions]);
  const activeView = searchParams.get("view");
  const activeProject = searchParams.get("project");

  // URL 项目参数同步到 store——直达 /?project= 时派单归属/过程槽 key
  // 依赖 store.activeProjectId（缺失会导致 @派单不触发）
  useEffect(() => {
    if (activeProject !== null) {
      useChatStore.getState().setActiveProject(activeProject);
    }
  }, [activeProject]);

  const openSession = useCallback(
    (session: SessionSummary) => {
      useChatStore.getState().setActiveSession(session.id);
      useChatStore.getState().setActiveProject(session.project_id || "");
      // 已读语义：done 圆点在用户打开该会话后清除（running 不清——在跑就得继续显示）
      const store = useChatStore.getState();
      if (shouldClearOnOpen(store.sessionActivity[session.id])) {
        store.clearSessionActivity(session.id);
      }
      // 员工单聊必须回到 DmChatView；普通会话才落会话视图（避免 DM 被当成助理流）
      if (isEmployeeDmSession(session) && session.target_role) {
        const project = session.project_id ? `&project=${encodeURIComponent(session.project_id)}` : "";
        navigate(`/chat?view=dm&target=${encodeURIComponent(session.target_role)}${project}`);
        return;
      }
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
      store.setActiveProject(task.project_id || "");
      navigate("/");
    },
    [navigate],
  );

  const deleteAutomationTask = useCallback(async (task: ScheduledTask) => {
    if (!window.confirm(`删除「${task.name}」及其运行会话历史？`)) return;
    try {
      await schedulerApi.remove(task.task_id);
      setAutomationTasks((current) => current.filter((item) => item.task_id !== task.task_id));
      toast.success("已删除自动化任务");
    } catch (error) {
      toast.error(errorMessage(error));
    }
  }, []);

  const newEmployeeSession = useCallback(
    (roleId: string, employeeName: string) => {
      const store = useChatStore.getState();
      store.setActiveProject("");
      store.clearSessionActivity(store.activeSessionId ?? "");
      navigate(`/chat?view=dm&target=${encodeURIComponent(roleId)}`);
      void employeeName;
    },
    [navigate],
  );

  /** 功能：项目行状态 = 该项目下所有会话的聚合（排除当前会话，与单条会话行同口径）。 */
  const projectActivity = useCallback(
    (projectId: string) =>
      aggregateSessionActivity(
        (grouped.get(projectId) ?? []).map((item) => item.id),
        sessionActivity,
        activeSessionId,
      ),
    [grouped, sessionActivity, activeSessionId],
  );

  /** 功能：项目内新建会话已改由顶部「＋新建会话」在项目上下文下触发。 */

  const exitSelection = () => {
    setSelectionMode(false);
    setSelectedIds(new Set());
  };

  const toggleSelected = (sessionId: string) => {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(sessionId)) next.delete(sessionId);
      else next.add(sessionId);
      return next;
    });
  };

  /** 功能：所有可见会话 id（任务区 + 各项目下，批量全选口径）。 */
  const allVisibleSessionIds = useMemo(
    () => [
      ...taskSessions.map((item) => item.id),
      ...projects.flatMap((project) =>
        (grouped.get(project.project_id) ?? []).map((item) => item.id),
      ),
    ],
    [taskSessions, projects, grouped],
  );

  const toggleAll = () => {
    setSelectedIds((previous) =>
      previous.size === allVisibleSessionIds.length && allVisibleSessionIds.length > 0
        ? new Set()
        : new Set(allVisibleSessionIds),
    );
  };

  /** 功能：项目下会话整组选择切换（项目行复选框，批量模式）。 */
  const toggleProject = (projectId: string) => {
    const ids = (grouped.get(projectId) ?? []).map((item) => item.id);
    if (ids.length === 0) return;
    setSelectedIds((previous) => {
      const next = new Set(previous);
      const allSelected = ids.every((id) => next.has(id));
      for (const id of ids) {
        if (allSelected) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  };

  const onDelete = async () => {
    if (!deleting) return;
    try {
      await sessionsApi.remove(deleting.id);
      useChatStore.getState().removeSessionLocal(deleting.id);
      // 第十一轮：删除反馈（此前单条删除静默执行，用户不知结果）
      toast.success(t("chat.session.deleted"));
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setDeleting(null);
    }
  };

  const onOpenFolder = async (project: ProjectRecord) => {
    try {
      await companyApi.openFolder(project.project_id);
      toast.success(t("chat.project.folderOpened"));
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const onRemoveProject = async () => {
    if (!removingProject) return;
    try {
      await companyApi.deleteProject(removingProject.project_id);
      // 语义：项目移出列表 = 解绑 + 旧会话不再显示（后端记录与磁盘文件不动）；
      // 记 id 快照后旧会话不会回落任务区，重新加入项目开的新会话照常显示
      useChatStore.getState().dismissProjectSessions(
        removingProject.project_id,
        (grouped.get(removingProject.project_id) ?? []).map((item) => item.id),
      );
      setProjects((previous) =>
        previous.filter((item) => item.project_id !== removingProject.project_id),
      );
      toast.success(t("chat.project.removed"));
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setRemovingProject(null);
    }
  };

  /** 功能：归档会话——项目会话沉淀摘要到项目 AGENTS.md；普通会话仅置状态。 */
  const archiveSession = async (session: SessionSummary) => {
    try {
      const result = await sessionsApi.archive(session.id);
      await refreshSessions();
      if (result.distilled) {
        toast.success(t("chat.toast.archivedToProject"));
      } else if (result.archived) {
        toast.success(t("chat.toast.archived"));
      } else {
        toast.info(t("chat.toast.archivedNoSummary").replace("{reason}", result.reason));
      }
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const bulkArchive = async () => {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    const results = await Promise.allSettled(ids.map((id) => sessionsApi.archive(id)));
    await refreshSessions();
    const failed = results.filter((item) => item.status === "rejected").length;
    if (failed > 0) {
      toast.error(t("chat.bulk.archiveFailed").replace("{n}", String(failed)));
    } else {
      toast.success(t("chat.bulk.archived").replace("{n}", String(ids.length)));
    }
    exitSelection();
  };

  const bulkDelete = async () => {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    setBulkDeleting(true);
    const removed = new Set<string>();
    try {
      for (const id of ids) {
        await sessionsApi.remove(id);
        useChatStore.getState().removeSessionLocal(id);
        removed.add(id);
      }
      toast.success(t("chat.bulk.deleted").replace("{n}", String(removed.size)));
      exitSelection();
    } catch (error) {
      // 中途失败：剔除已删项、保留未删项供重试（-3 修复）
      if (removed.size > 0) {
        setSelectedIds((previous) => {
          const next = new Set(previous);
          removed.forEach((id) => next.delete(id));
          return next;
        });
      }
      toast.error(errorMessage(error));
    } finally {
      setBulkDeleting(false);
    }
  };

  const sessionMenuItems = (session: SessionSummary) => (
    <>
      <DropdownMenuItem onClick={() => useChatStore.getState().togglePin(session.id)}>
        {pinnedSet.has(session.id) ? <PinOff /> : <Pin />}
        {pinnedSet.has(session.id) ? t("chat.session.unpin") : t("chat.session.pin")}
      </DropdownMenuItem>
      <DropdownMenuItem onClick={() => void archiveSession(session)}>
        <Archive /> {t("chat.session.archive")}
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem onClick={() => setRenaming(session)}>
        <Pencil /> {t("chat.session.rename")}
      </DropdownMenuItem>
      <DropdownMenuItem
        onClick={() => {
          setSelectionMode(true);
          setSelectedIds(new Set([session.id]));
        }}
      >
        <ListChecks /> {t("chat.bulk.enter")}
      </DropdownMenuItem>
      <DropdownMenuItem variant="destructive" onClick={() => setDeleting(session)}>
        <Trash2 /> {t("chat.session.delete")}
      </DropdownMenuItem>
    </>
  );

  const renderSessionRow = (session: SessionSummary) =>
    selectionMode ? (
      <label
        key={session.id}
        className="hover:bg-primary/5 flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs"
      >
        <BulkCheckbox
          checked={selectedIds.has(session.id)}
          onChange={() => toggleSelected(session.id)}
        />
        <span className="min-w-0 flex-1 truncate">
          {session.title?.trim() || `${session.id.slice(0, 8)}…`}
        </span>
      </label>
    ) : (
      <div
        key={session.id}
        className={cn(
          "group text-muted-foreground hover:bg-primary/5 hover:text-foreground relative flex w-full items-center gap-1 rounded-lg pr-1",
          activeSessionId === session.id && onChatRoute && !activeView && ACTIVE_CLASS,
        )}
        onMouseEnter={() => setHoveredId(session.id)}
        onMouseLeave={() => setHoveredId((prev) => (prev === session.id ? "" : prev))}
      >
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left text-xs"
          title={t("chat.session.messageCount").replace("{n}", String(session.message_count))}
          onClick={() => openSession(session)}
        >
          {pinnedSet.has(session.id) ? <Pin className="text-primary size-3 shrink-0" /> : null}
          <span className="min-w-0 flex-1 truncate">
            {session.title?.trim() || `${session.id.slice(0, 8)}…`}
          </span>
        </button>
        {/* 功能：更多按钮平时隐形（按钮结构常驻），hover / 聚焦时浮现；
            活动圆点占据其默认位置，hover 时左移到按钮左侧。当前会话不显示圆点。 */}
        <span
          data-testid={`session-activity-slot-${session.id}`}
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
            testId={`session-activity-${session.id}`}
          />
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              data-testid="session-more-trigger"
              className="no-focus-ring text-chat-muted/60 hover:text-foreground inline-flex size-5 shrink-0 items-center justify-center rounded-md"
              title={t("common.more")}
              style={{
                opacity: hoveredId === session.id ? 1 : 0,
                pointerEvents: hoveredId === session.id ? "auto" : "none",
                transition: "opacity 120ms ease-out",
              }}
              onFocus={() => setHoveredId(session.id)}
              onBlur={() => setHoveredId((prev) => (prev === session.id ? "" : prev))}
            >
              <MoreHorizontal className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">{sessionMenuItems(session)}</DropdownMenuContent>
        </DropdownMenu>
      </div>
    );

  const renderMoreToggle = (
    total: number,
    expanded: boolean,
    onToggle: () => void,
    testId: string,
  ) =>
    total > SECTION_LIMIT ? (
      <button
        type="button"
        data-testid={testId}
        className="text-chat-muted hover:text-foreground w-full px-2 py-1 text-left text-[11px]"
        onClick={onToggle}
      >
        {expanded
          ? t("chat.showLess")
          : t("chat.showMore").replace("{n}", String(total - SECTION_LIMIT))}
      </button>
    ) : null;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col" data-testid="sidebar-context-list">
      <div className="min-h-0 flex-1 overflow-y-auto px-1 py-2">
        {/* 「任务」段：无项目归属的普通会话（+ 无主项目会话兜底；项目会话不进任务区） */}
        <div className="mb-3">
          <SectionHeader
            label={t("chat.sectionTasks")}
            count={taskSessions.length}
            open={tasksOpen}
            onToggle={toggleTasksOpen}
            testId="section-tasks"
          />
          {tasksOpen ? (
            taskSessions.length === 0 ? null : (
              <>
                <div className="space-y-0.5">
                  {(tasksAll ? taskPlainSessions : taskPlainSessions.slice(0, SECTION_LIMIT)).map(
                    renderSessionRow,
                  )}
                  {Array.from(taskEmployeeGroups.entries()).map(([roleId, group]) => {
                    const employee = employeeByRole.get(roleId);
                    const name = employee?.name || group[0]?.employee_name || roleId;
                    const visible = tasksAll ? group : group.slice(0, SECTION_LIMIT);
                    return (
                      <div key={`employee-${roleId}`} className="mt-1">
                        <div
                          data-testid={`task-employee-group-${roleId}`}
                          className="text-muted-foreground hover:bg-primary/5 flex w-full items-center gap-1.5 rounded-lg py-1 pr-1 pl-2"
                        >
                          <EmployeeAvatar
                            name={name}
                            metadata={employee?.metadata}
                            className="size-6 rounded-full text-[10px]"
                          />
                          <button
                            type="button"
                            className="min-w-0 flex-1 truncate text-left text-xs"
                            onClick={() => openSession(group[0])}
                          >
                            {name}
                          </button>
                          <SessionActivityIcon
                            state={aggregateSessionActivity(
                              group.map((item) => item.id),
                              sessionActivity,
                              activeSessionId,
                            )}
                            testId={`task-employee-activity-${roleId}`}
                          />
                          <button
                            type="button"
                            data-testid={`task-employee-new-${roleId}`}
                            className="text-chat-muted/60 hover:text-foreground flex size-5 items-center justify-center rounded-md"
                            title={t("nav.newTask")}
                            onClick={() => void newEmployeeSession(roleId, name)}
                          >
                            <NewSessionIcon className="size-3.5" />
                          </button>
                        </div>
                        <div className="space-y-0.5 pl-[38px]">{visible.map(renderSessionRow)}</div>
                      </div>
                    );
                  })}
                </div>
                {renderMoreToggle(taskSessions.length, tasksAll, toggleTasksAll, "tasks-more")}
              </>
            )
          ) : null}
        </div>

        {/* 「项目空间」段：项目 = 团队/项目入口（点击进项目总群聊）；项目下不展开会话列表（项目面板内查看） */}
        <div className="mb-2">
          <SectionHeader
            label={t("chat.sectionProjects")}
            count={projects.length}
            open={projectsOpen}
            onToggle={toggleProjectsOpen}
            testId="section-projects"
          />
          {projectsOpen ? (
            projects.length === 0 ? (
              <p className="text-chat-muted px-2 py-1 text-[11px]">
                {t("chat.projectsEmptyShort")}
              </p>
            ) : (
              <>
                {(projectsAll ? projects : projects.slice(0, SECTION_LIMIT)).map((project) => (
                  <div key={project.project_id} className="mb-1.5">
                    <div
                      className={cn(
                        "group text-muted-foreground hover:bg-primary/5 hover:text-foreground relative flex w-full items-center gap-1 rounded-lg pr-1",
                        activeProject === project.project_id && onChatRoute && ACTIVE_CLASS,
                      )}
                      onMouseEnter={() => setHoveredId(`project:${project.project_id}`)}
                      onMouseLeave={() =>
                        setHoveredId((prev) =>
                          prev === `project:${project.project_id}` ? "" : prev,
                        )
                      }
                    >
                      <button
                        type="button"
                        className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left text-xs font-medium"
                        title={
                          selectionMode ? t("chat.bulk.selectAll") : t("chat.projectGroupHint")
                        }
                        onClick={() => {
                          if (selectionMode) {
                            toggleProject(project.project_id);
                            return;
                          }
                          // 点项目默认打开该项目「最新时间的会话记录」；
                          // .3 删甲：无任何会话时进入团队群聊会话新建态（不再跳群聊独立视图）
                          const projectSessions = [...(grouped.get(project.project_id) ?? [])].sort(
                            (a, b) => (b.created_at || "").localeCompare(a.created_at || ""),
                          );
                          const latest = projectSessions[0];
                          if (latest) {
                            openSession(latest);
                          } else {
                            const store = useChatStore.getState();
                            store.setActiveSession(null);
                            store.setActiveProject(project.project_id);
                            store.setPendingSessionOrigin("");
                            navigate(`/?project=${project.project_id}`);
                          }
                        }}
                      >
                        {selectionMode ? (
                          <BulkCheckbox
                            checked={(grouped.get(project.project_id) ?? []).every((item) =>
                              selectedIds.has(item.id),
                            )}
                            onChange={() => toggleProject(project.project_id)}
                            testId="project-select-box"
                            stopPropagation
                          />
                        ) : null}
                        <Folder className="text-chat-muted size-3.5 shrink-0" />
                        <span className="min-w-0 flex-1 truncate">
                          {project.name || project.project_id}
                        </span>
                        {/* 2026-10-02 删除项目条目右侧的成员数显示 */}
                      </button>
                      {/* 右侧槽位（与会话行同口径）：非悬浮显示任务状态；悬浮时
                          「更多」按钮浮现，状态点左移让位 */}
                      <span
                        data-testid={`project-activity-slot-${project.project_id}`}
                        aria-hidden
                        className="pointer-events-none absolute top-1/2 -translate-y-1/2"
                        style={{
                          right: hoveredId === `project:${project.project_id}` ? 30 : 8,
                          transition: "right 120ms ease-out",
                        }}
                      >
                        <SessionActivityIcon
                          state={projectActivity(project.project_id)}
                          testId={`project-activity-${project.project_id}`}
                        />
                      </span>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <button
                            type="button"
                            data-testid="project-menu-trigger"
                            className="no-focus-ring text-chat-muted/60 hover:text-foreground flex size-5 shrink-0 items-center justify-center rounded-md"
                            title={t("common.more")}
                            style={{
                              opacity: hoveredId === `project:${project.project_id}` ? 1 : 0,
                              pointerEvents:
                                hoveredId === `project:${project.project_id}` ? "auto" : "none",
                              transition: "opacity 120ms ease-out",
                            }}
                            onFocus={() => setHoveredId(`project:${project.project_id}`)}
                            onBlur={() =>
                              setHoveredId((prev) =>
                                prev === `project:${project.project_id}` ? "" : prev,
                              )
                            }
                          >
                            <MoreHorizontal className="size-3.5" />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end">
                          <DropdownMenuItem
                            data-testid="project-menu-rename"
                            onClick={() => setRenamingProject(project)}
                          >
                            <Pencil /> {t("chat.project.rename")}
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => void onOpenFolder(project)}>
                            <FolderOpen /> {t("chat.project.openFolder")}
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem
                            data-testid="project-menu-remove"
                            variant="destructive"
                            onClick={() => setRemovingProject(project)}
                          >
                            <Trash2 /> {t("chat.project.remove")}
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </div>
                  </div>
                ))}
                {renderMoreToggle(projects.length, projectsAll, toggleProjectsAll, "projects-more")}
              </>
            )
          ) : null}
        </div>

        {/* 「自动化」段：仅无项目任务；点击进入普通会话，X 删除任务 */}
        {automationGroups.length > 0 ? (
          <div className="mb-2">
            <SectionHeader
              label="自动化"
              count={automationGroups.reduce((total, group) => total + group.tasks.length, 0)}
              open={automationOpen}
              onToggle={() => setAutomationOpen((value) => !value)}
              testId="section-automation"
            />
            {automationOpen
              ? automationGroups.map((group) => (
                  <div key={group.key} className="mt-1">
                    <div
                      data-testid={`automation-employee-${group.key}`}
                      className="text-muted-foreground flex w-full items-center gap-1.5 rounded-lg py-1 pr-1 pl-2"
                    >
                      {group.key === "assistant" ? (
                        <AssistantAvatar className="size-6" />
                      ) : (
                        <EmployeeAvatar
                          name={group.name}
                          metadata={group.employee?.metadata}
                          className="size-6 rounded-full text-[10px]"
                        />
                      )}
                      <span className="min-w-0 flex-1 truncate text-xs">{group.name}</span>
                      <span className="text-chat-muted text-[10px]">{group.tasks.length}</span>
                    </div>
                    <div className="space-y-0.5 pl-[38px]">
                      {group.tasks.map((task) => (
                        <div
                          key={task.task_id}
                          className={cn(
                            "group text-muted-foreground hover:bg-primary/5 hover:text-foreground relative flex w-full items-center gap-1 rounded-lg pr-1",
                            activeSessionId === task.session_id && onChatRoute && ACTIVE_CLASS,
                          )}
                        >
                          <button
                            type="button"
                            data-testid={`automation-task-${task.task_id}`}
                            className="flex min-w-0 flex-1 items-center gap-1.5 py-1 text-left"
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
                            data-testid={`automation-delete-${task.task_id}`}
                            className="text-chat-muted/60 hover:text-destructive flex size-4 shrink-0 items-center justify-center rounded opacity-0 transition-opacity group-hover:opacity-100"
                            title="删除自动化任务"
                            onClick={(event) => {
                              event.stopPropagation();
                              void deleteAutomationTask(task);
                            }}
                          >
                            <Trash2 className="size-3" />
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                ))
              : null}
          </div>
        ) : null}
      </div>

      {/* 2026-09-25 批量操作改为面板内分区（贴合面板两边，非弹窗） */}
      {selectionMode ? (
        <div
          className="border-primary/5 bg-card shrink-0 border-t px-2 py-2"
          data-testid="bulk-panel"
        >
          <div className="flex items-center gap-2 px-2.5 pt-2">
            <label className="flex cursor-pointer items-center gap-1.5 text-[11px]">
              <BulkCheckbox
                checked={
                  allVisibleSessionIds.length > 0 &&
                  selectedIds.size === allVisibleSessionIds.length
                }
                onChange={toggleAll}
                testId="bulk-select-all"
              />
              {t("chat.bulk.selectAll")}
            </label>
            <span className="text-muted-foreground flex-1 truncate text-[11px]">
              {t("chat.bulk.selected").replace("{n}", String(selectedIds.size))}
            </span>
            <Button
              variant="ghost"
              size="icon-xs"
              title={t("common.cancel")}
              onClick={exitSelection}
            >
              <X className="size-3.5" />
            </Button>
          </div>
          <div className="flex gap-2 p-2">
            <Button
              variant="destructive"
              className="border-primary/5 h-8 flex-1 gap-1.5 rounded-xl"
              data-testid="bulk-delete-btn"
              disabled={selectedIds.size === 0 || bulkDeleting}
              onClick={() => setBulkConfirmOpen(true)}
            >
              <Trash2 className="size-3.5" />
              {t("chat.bulk.delete")}
            </Button>
            <Button
              variant="secondary"
              className="border-primary/5 h-8 flex-1 gap-1.5 rounded-xl"
              data-testid="bulk-archive-btn"
              disabled={selectedIds.size === 0}
              onClick={bulkArchive}
            >
              <Archive className="size-3.5" />
              {t("chat.bulk.archive")}
            </Button>
          </div>
        </div>
      ) : null}

      <RenameSessionDialog session={renaming} onOpenChange={(open) => !open && setRenaming(null)} />
      <RenameProjectDialog
        project={renamingProject}
        onRenamed={(updated) =>
          setProjects((previous) =>
            previous.map((item) => (item.project_id === updated.project_id ? updated : item)),
          )
        }
        onOpenChange={(open) => !open && setRenamingProject(null)}
      />

      <AlertDialog open={deleting !== null} onOpenChange={(open) => !open && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("chat.session.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("chat.session.deleteHint")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction data-testid="session-delete-confirm" onClick={() => void onDelete()}>
              {t("chat.session.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={removingProject !== null}
        onOpenChange={(open) => !open && setRemovingProject(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("chat.project.removeTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("chat.project.removeHint")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              data-testid="project-remove-confirm"
              onClick={() => void onRemoveProject()}
            >
              {t("chat.project.remove")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={bulkConfirmOpen} onOpenChange={setBulkConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("chat.bulk.deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("chat.bulk.deleteHint").replace("{n}", String(selectedIds.size))}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction data-testid="bulk-delete-confirm" onClick={() => void bulkDelete()}>
              {t("chat.bulk.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
