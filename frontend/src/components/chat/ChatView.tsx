/**
 * 功能：对话主视图（@base docs/前端设计规划.md §6 会话行 / §8 交互规范）。
 * 组成：会话详情加载 + 消息流（keyed 增量）+ 过程块 + 中断恢复提示 + 回滚暂存条 +
 * 挂起交互卡 + Composer（发送/转向/停止）。群聊·单聊视图见 pages/chat 分流。
 */
import { CircleAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Composer } from "@/components/chat/Composer";
import { EmployeePickerDialog } from "@/components/chat/EmployeePickerDialog";
import { GroupRoundBar } from "@/components/chat/GroupRoundBar";
import { InteractionCard, type InteractionDraft } from "@/components/chat/InteractionCard";
import { PendingEmployeeReply } from "@/components/chat/PendingEmployeeReply";
import { LiveProgressList } from "@/components/chat/LiveProgressList";
import { APPROVAL_FRAME_PREFIX, useCompanyApprovals } from "@/hooks/use-company-approvals";
import { MessageList } from "@/components/chat/MessageList";
import { ProjectSpaceCard } from "@/components/chat/ProjectSpaceCard";
import { RevertBanner } from "@/components/chat/RevertBanner";
import { Button } from "@/components/ui/button";
import { useEmployees } from "@/hooks/use-employees";
import { useT } from "@/i18n";
import {
  findEmployeeByName,
  resolveEmployeeName,
  SECRETARY_ROLE_ID,
  type AssistantIdentity,
} from "@/lib/employee";
import { interruptionHintKey, interruptionTier, tierAllowsRetry } from "@/lib/interruption";
import { normalizeProjectScope } from "@/lib/progress-key";
import { cn } from "@/lib/utils";
import { ApiError } from "@/services/api";
import { companyApi, type EmployeeSummary, type WorkItemSummary } from "@/services/company";
import { panelApi, type ProjectArtifact } from "@/services/panel";
import { sessionsApi } from "@/services/sessions";
import { sessionSocket, socket } from "@/services/ws";
import {
  entriesToMessages,
  sessionChatMode,
  sessionTaskLedger,
  settleRunningNodes,
  useChatStore,
  type ChatAttachment,
  type ChatMessage,
} from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";
import { useUiStore } from "@/stores/ui-store";

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** 稳定空引用（zustand selector 返回值必须引用稳定，否则触发无限重渲）。 */
const EMPTY_MESSAGES: ChatMessage[] = [];

/** 停止受理后等待终结帧的窗口（超时未见 done → 本地兜底定稿，防残留）。 */
const ABORT_SETTLE_WINDOW_MS = 2500;

/** 功能：新会话 id（优先 crypto.randomUUID；非安全上下文回退时间戳+随机）。 */
function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `sess-${crypto.randomUUID()}`;
  }
  return `sess-${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
}

export function ChatView() {
  const t = useT();
  const { employees, loaded } = useEmployees();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const activeProjectId = useChatStore((state) => state.activeProjectId);
  const activeRound = useChatStore((state) =>
    activeSessionId ? state.roundsBySession[activeSessionId] : undefined,
  );
  const streams = useChatStore((state) => state.streams);
  const stagedRevert = useChatStore((state) => state.stagedRevert);
  const interactions = useChatStore((state) => state.interactions);
  // 员工 headless 审批数据桥（ApprovalStore pending → 交互帧）
  useCompanyApprovals();
  const messages = useChatStore(
    (state) =>
      (activeSessionId ? state.messagesBySession[activeSessionId] : state.messagesBySession[""]) ??
      EMPTY_MESSAGES,
  );

  const [dismissedResume, setDismissedResume] = useState("");
  const sessionKey = activeSessionId ?? "";
  // busy 按会话判定——每会话独立流槽，其它会话的流不影响本会话。
  const busy = Boolean(streams[sessionKey]);

  // 空态秘书提示的团队判定——与项目面板 hasTeam 同源（项目成员数）；
  // 无项目归属时回落全局员工列表（保持原有语义）
  const [projectHasTeam, setProjectHasTeam] = useState<boolean | null>(null);
  const [projectDissolved, setProjectDissolved] = useState(false);
  const [projectDissolvedTeamId, setProjectDissolvedTeamId] = useState("");
  const [projectLocked, setProjectLocked] = useState(false);
  // ⊕「选择员工�?@ 面板员工组仅项目群聊会话可见——需要成�?role_ids�?
  // 项目内秘书会话与普通会话不消费
  const [projectMemberRoles, setProjectMemberRoles] = useState<string[]>([]);
  const sessions = useChatStore((state) => state.sessions);
  const activeSession = sessions.find((item) => item.id === activeSessionId) ?? null;
  const isSecretarySession = activeSession?.origin === "secretary";
  const activeSessionTargetRole = String(activeSession?.target_role ?? "");
  const isEmployeeDmSession =
    activeSession?.origin === "employee_dm" || Boolean(activeSessionTargetRole);
  const allowMemberMention = Boolean(activeProjectId) && !isSecretarySession;
  const projectHasGroupChat =
    allowMemberMention && (projectHasTeam === true || projectMemberRoles.length > 0);
  const [pendingEmployee, setPendingEmployee] = useState<EmployeeSummary | null>(null);
  const [employeePickerOpen, setEmployeePickerOpen] = useState(false);
  const [pendingReply, setPendingReply] = useState<{
    roleId: string;
    name: string;
    metadata?: Record<string, unknown>;
  } | null>(null);
  const composerEmployee = useMemo(() => {
    if (pendingEmployee) return pendingEmployee;
    if (messages.length === 0 && activeSessionTargetRole) {
      return (
        employees.find(
          (employee) =>
            employee.role_id === activeSessionTargetRole ||
            employee.employee_id === activeSession?.employee_id,
        ) ?? null
      );
    }
    return null;
  }, [
    pendingEmployee,
    messages.length,
    activeSessionTargetRole,
    activeSession?.employee_id,
    employees,
  ]);
  const employeeOptions = useMemo(() => {
    if (activeProjectId && projectMemberRoles.length) {
      const roles = new Set(projectMemberRoles);
      return employees.filter((employee) => roles.has(employee.role_id));
    }
    return employees;
  }, [activeProjectId, employees, projectMemberRoles]);
  const pendingReplyEmployee = useMemo(() => {
    if (pendingReply) return pendingReply;
    const speaker = activeRound?.slots.find((slot) => slot.status === "speaking");
    if (!speaker) return null;
    const employee = employees.find((item) => item.role_id === speaker.role_id);
    return {
      roleId: speaker.role_id,
      name: speaker.role_name || employee?.name || speaker.role_id,
      metadata: employee?.metadata,
    };
  }, [pendingReply, activeRound, employees]);
  useEffect(() => {
    if (!pendingReply) return;
    const last = messages[messages.length - 1];
    if (last?.role === "assistant" && last.senderRole === pendingReply.roleId) {
      setPendingReply(null);
    }
  }, [messages, pendingReply]);
  useEffect(() => {
    if (!activeProjectId) {
      setProjectHasTeam(null);
      setProjectDissolved(false);
      setProjectDissolvedTeamId("");
      setProjectLocked(false);
      setProjectMemberRoles([]);
      return;
    }
    let cancelled = false;
    const loadProjectMembers = () => {
      void companyApi
        .projects()
        .then(({ projects }) => {
          if (cancelled) return;
          const project = projects.find((item) => item.project_id === activeProjectId);
          setProjectHasTeam((project?.members.length ?? 0) > 0);
          const dissolvedTeams = project?.dissolved_team_ids ?? [];
          setProjectDissolved((project?.team_ids?.length ?? 0) === 0 && dissolvedTeams.length > 0);
          setProjectDissolvedTeamId(dissolvedTeams[0] ?? "");
          setProjectMemberRoles(
            (project?.members ?? [])
              .filter((member) => member.employee_status === "active")
              .map((member) => member.role_id),
          );
        })
        .catch(() => undefined);
    };
    loadProjectMembers();
    window.addEventListener("bandi:project-teams-changed", loadProjectMembers);
    return () => {
      cancelled = true;
      window.removeEventListener("bandi:project-teams-changed", loadProjectMembers);
    };
  }, [activeProjectId]);

  useEffect(() => {
    if (
      projectDissolved &&
      messages.some(
        (message) => message.role === "system" && message.content === t("chat.note.teamDissolved"),
      )
    ) {
      setProjectLocked(true);
    }
  }, [messages, projectDissolved, t]);

  // 会话内搜索/历史提问发布到顶栏槽位（AppHeader 渲染）
  const toolItems = useMemo(
    () =>
      messages.map((message) => ({
        id: message.id,
        role: message.role,
        content: message.content,
      })),
    [messages],
  );
  // v5 @提及名单（员工名 + 角色 id——消息内 @ 高亮且可点击联动右栏）
  const mentionNames = useMemo(
    () => employees.flatMap((item) => [item.name, item.role_id]),
    [employees],
  );
  useEffect(() => {
    useUiStore.getState().setHeaderTools(toolItems, "chat-tools");
    return () => useUiStore.getState().clearHeaderTools();
  }, [toolItems]);

  // 会话详情加载（切换会话时读回历史；流式 pending 由 store 合并保留）
  useEffect(() => {
    if (!activeSessionId) return;
    let cancelled = false;
    sessionsApi
      .detail(activeSessionId)
      .then((detail) => {
        if (cancelled) return;
        useChatStore
          .getState()
          .loadMessages(activeSessionId, entriesToMessages(activeSessionId, detail.messages));
        // Day137b-1：任务台账从会话 metadata 恢复（刷新后任务条不消失）
        useChatStore.getState().setTaskLedger(activeSessionId, sessionTaskLedger(detail.session));
        // 模式从会话持久值恢复（sessions.mode；无持久值时保留当前选择）
        const persistedMode = detail.session?.mode;
        if (typeof persistedMode === "string" && persistedMode.trim()) {
          useUiStore.getState().setChatMode(sessionChatMode(detail.session));
        }
        // P3：挂起交互重放——刷新/重连后 pending 决策卡补拉（无重放则卡永久丢失）
        sessionsApi
          .pendingInteractions(activeSessionId)
          .then((payload) => {
            if (cancelled) return;
            for (const frame of payload.interactions ?? []) {
              if (frame.interaction_id) {
                useChatStore.getState().addInteraction(frame);
              }
            }
          })
          .catch(() => undefined); // 重放失败不阻断会话加载（下次进入可再试）
      })
      .catch((error) => {
        if (cancelled) return;
        // 恢复的会话 id 已失效（换库/已删除）→ 清引用回「新会话」态，避免继续往幽灵 id 发消息
        if (error instanceof ApiError && error.status === 404) {
          useChatStore.getState().setActiveSession(null);
          return;
        }
        toast.error(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  useEffect(() => {
    if (!activeSessionId) return;
    let cancelled = false;
    companyApi
      .round(activeSessionId, activeProjectId)
      .then(({ round }) => {
        if (cancelled) return;
        if (round) useChatStore.getState().applyRound(round);
        else useChatStore.getState().clearRound(activeSessionId);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, activeProjectId]);

  // 团队会话：监听群聊、员工回复、线程回写等消息，维护消息头像+名字。
  useEffect(() => {
    return socket.on("thread_message", (frame) => {
      const data =
        frame.data && typeof frame.data === "object" ? (frame.data as Record<string, unknown>) : {};
      const sessionId = useChatStore.getState().activeSessionId;
      if (!sessionId) return;
      const active = useChatStore.getState().sessions.find((item) => item.id === sessionId);
      if (!active || active.origin === "secretary") return;
      // 项目口径统一走 normalizeProjectScope（与写侧/其余视图同口径）；
      // 任务单聊无项目时只按线程过滤，不按空 project 直接丢弃。
      if (
        activeProjectId &&
        normalizeProjectScope(String(data.project_id ?? "")) !==
          normalizeProjectScope(activeProjectId)
      )
        return;
      const incomingSession = String(data.session_id ?? "");
      if (incomingSession && incomingSession !== sessionId) return;
      const expectedThread = String(active.thread_id ?? "");
      const incomingThread = String(data.thread_id ?? "");
      if (expectedThread && incomingThread && expectedThread !== incomingThread) return;
      const sender = String(data.sender ?? "");
      if (!sender || sender === "user") return;
      if (active.target_role && sender !== active.target_role) return;
      // 功能：员工回复落位——清掉「已转交团队员工，等待接单执行…」过渡注记
      useChatStore.getState().clearTransientNotes(sessionId);
      useChatStore.getState().appendEmployeeReply(sessionId, {
        name: resolveEmployeeName(employees, sender),
        roleId: sender,
        content: String(data.content ?? ""),
        // 成员任务卡关联键（线程消息 message_id = work item id）
        workItemId: String(data.message_id ?? "").trim() || undefined,
        // Day137b-1：员工执行过程随回复固化（回复消息上方过程块；槽取出即清）
        // 槽键含 thread（= 这一次执行的身份，与产出回写同源）
        // 落定即结算 running（回写是执行中途快照，残留 running 是死状态）
        process: settleRunningNodes(
          useChatStore
            .getState()
            .consumeEmployeeProgress(
              normalizeProjectScope(String(data.project_id ?? "")),
              String(data.thread_id ?? ""),
              sender,
            ),
        ),
      });
      setPendingReply((current) => (current?.roleId === sender ? null : current));
    });
  }, [activeProjectId, employees]);

  // 成员任务卡数据源：当前项目工作项（员工回复按 workItemId → parent_id 关联子卡——
  // 「成员任务」块：标题/指派给/任务条目折叠展开；串行每环一块、并行并列）。
  const [projectWorkItems, setProjectWorkItems] = useState<WorkItemSummary[]>([]);
  // 功能：项目全量工作项（含聊天卡）——停止键与「正在执行」判定以它为准
  const [projectRawItems, setProjectRawItems] = useState<WorkItemSummary[]>([]);
  const [tasksRefreshTick, setTasksRefreshTick] = useState(0);
  const activeSessionIdForTasks = useChatStore((state) => state.activeSessionId);
  const messageCountForTasks = useChatStore(
    (state) => state.messagesBySession[activeSessionIdForTasks ?? ""]?.length ?? 0,
  );
  useEffect(() => {
    if (!activeProjectId) {
      setProjectWorkItems([]);
      setProjectRawItems([]);
      return;
    }
    let cancelled = false;
    const load = () => {
      void companyApi
        .workItems()
        .then((result) => {
          if (cancelled) return;
          const all = result.items.filter((item) => item.project_id === activeProjectId);
          setProjectRawItems(all);
          setProjectWorkItems(
            all.filter(
              // 与右栏计划同口径：排除聊天会话卡（chat_mention/chat_dm）与咨询问答卡
              // （chat_kind=query——纯问答不出发计划），其余工作项（agent_to_agent
              // 分工卡等）一致可见——右栏与任务卡同源。
              (item) =>
                item.source !== "chat_mention" &&
                item.source !== "chat_dm" &&
                item.chat_kind !== "query",
            ),
          );
          // 功能：后端无任何 running 工作项时结算前端残留的执行过程帧
          // （清理/重启后旧帧无 done 帧，会永远显示「正在执行」）
          if (all.every((item) => item.phase !== "running")) {
            const state = useChatStore.getState();
            const settled = state.settleStaleEmployeeProgress();
            for (const key of settled) {
              window.setTimeout(() => {
                useChatStore.getState().expireEmployeeProgressByKey(key);
              }, 2500);
            }
          }
        })
        .catch(() => undefined);
    };
    load();
    // 10s 轮询：自动建链/派卡发生在回复之后，仅靠消息数变化会漏刷（成员卡不显示）
    const timer = window.setInterval(load, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // 消息数变化重拉（派卡发生在执行中；新回复到达即应看到新子卡）
  }, [activeProjectId, messageCountForTasks, tasksRefreshTick]);
  // 功能：群聊有员工任务正在执行（running）——Composer 显示停止键 + 双 Esc 可停止
  const teamRunningItems = useMemo(
    () => projectRawItems.filter((item) => item.phase === "running"),
    [projectRawItems],
  );
  const teamRunning = teamRunningItems.length > 0;
  const stopTeam = async () => {
    if (!teamRunningItems.length) return;
    const results = await Promise.allSettled(
      teamRunningItems.map((item) => companyApi.abortWorkItem(item.id)),
    );
    const interrupted = results.filter(
      (result) => result.status === "fulfilled" && result.value.interrupted,
    ).length;
    if (interrupted > 0) toast.info(t("chat.composer.teamStopped"));
    setTasksRefreshTick((value) => value + 1);
  };
  const memberTasks = useMemo(() => {
    const byParent = new Map<string, WorkItemSummary[]>();
    for (const item of projectWorkItems) {
      const parent = String(item.parent_id ?? "");
      if (!parent) continue;
      byParent.set(parent, [...(byParent.get(parent) ?? []), item]);
    }
    const out: Record<string, WorkItemSummary[]> = {};
    for (const message of messages) {
      if (message.workItemId && byParent.has(message.workItemId)) {
        out[message.id] = byParent.get(message.workItemId)!;
      }
    }
    return out;
  }, [projectWorkItems, messages]);

  // 交付物卡片数据源：项目产物按 work_item_id 关联到员工回复（回复下方显示，点击开右栏）
  const [projectArtifacts, setProjectArtifacts] = useState<ProjectArtifact[]>([]);
  useEffect(() => {
    if (!activeProjectId) {
      setProjectArtifacts([]);
      return;
    }
    let cancelled = false;
    void panelApi
      .projectArtifacts(activeProjectId)
      .then((result) => {
        if (!cancelled) setProjectArtifacts(result.artifacts);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [activeProjectId, messageCountForTasks]);
  const replyArtifacts = useMemo(() => {
    const byWorkItem = new Map<string, ProjectArtifact[]>();
    for (const artifact of projectArtifacts) {
      const workItem = String(artifact.work_item_id ?? "");
      if (!workItem) continue;
      byWorkItem.set(workItem, [...(byWorkItem.get(workItem) ?? []), artifact]);
    }
    const out: Record<string, ProjectArtifact[]> = {};
    for (const message of messages) {
      if (message.workItemId && byWorkItem.has(message.workItemId)) {
        out[message.id] = byWorkItem.get(message.workItemId)!;
      }
    }
    return out;
  }, [projectArtifacts, messages]);

  // ── v5：会话右栏已提升为通高第四栏（SessionPanelHost 挂 AppShell）；本视图只保留任务条联动 ──

  const send = (text: string, attachments?: ChatAttachment[]) => {
    const store = useChatStore.getState();
    // 流槽按会话隔离——别的会话回复中不再阻塞本会话发送；
    // 仅本会话仍在回复时兜底拒绝（正常由 Composer busy→steer 处理）。
    if (activeSessionId && store.streams[activeSessionId]) {
      return;
    }
    if (projectDissolved && activeSessionId && !isSecretarySession) {
      void sessionsApi
        .appendSystemNote(
          activeSessionId,
          t("chat.note.teamDissolved"),
          "team_dissolved",
          `team_dissolved:${projectDissolvedTeamId || activeProjectId}`,
        )
        .then(() => sessionsApi.detail(activeSessionId))
        .then((detail) => {
          store.loadMessages(activeSessionId, entriesToMessages(activeSessionId, detail.messages));
          setProjectLocked(true);
        })
        .catch((error) => toast.error(errorMessage(error)));
      return;
    }
    // 会话 id 有效性校验：列表已加载但查无此会话（persist 恢复的幽灵 id）→ 视为新会话
    const known =
      activeSessionId !== null &&
      (!store.sessionsLoaded || store.sessions.some((item) => item.id === activeSessionId));
    const sessionId = known && activeSessionId ? activeSessionId : newSessionId();
    const pendingOrigin = store.pendingSessionOrigin;
    const dmTargetRole = isEmployeeDmSession
      ? activeSessionTargetRole
      : (pendingEmployee?.role_id ?? "");
    const dmEmployee =
      (dmTargetRole
        ? employees.find((employee) => employee.role_id === dmTargetRole)
        : null) ??
      (pendingEmployee?.role_id === dmTargetRole ? pendingEmployee : null);
    const mentionedRole = text.match(/@([\w-]+)/)?.[1] ?? "";
    const groupTargetRole =
      !dmTargetRole && projectHasGroupChat
        ? projectMemberRoles.includes(mentionedRole)
          ? mentionedRole
          : (projectMemberRoles[0] ?? "")
        : "";
    const optimisticRole = dmTargetRole || groupTargetRole;
    const optimisticEmployee =
      employees.find((employee) => employee.role_id === optimisticRole) ?? dmEmployee;
    if (optimisticRole) {
      setPendingReply({
        roleId: optimisticRole,
        name: optimisticEmployee?.name ?? optimisticRole,
        metadata: optimisticEmployee?.metadata,
      });
    }
    let title = "";
    if (!known) {
      title = text.slice(0, 40);
      store.setActiveSession(sessionId);
      store.upsertSession({
        id: sessionId,
        title,
        created_at: new Date().toISOString(),
        message_count: 0,
        project_id: activeProjectId,
        ...(dmTargetRole
          ? {
              origin: "employee_dm",
              target_role: dmTargetRole,
              ...(dmEmployee?.employee_id ? { employee_id: dmEmployee.employee_id } : {}),
              ...(dmEmployee?.name ? { employee_name: dmEmployee.name } : {}),
            }
          : pendingOrigin
            ? { origin: pendingOrigin }
            : isNewSession && activeProjectId
              ? { origin: "secretary" }
              : {}),
      });
    }
    store.appendLocalUser(sessionId, text, attachments);
    store.markSessionActivity(sessionId, "running");
    const ui = useUiStore.getState();
    sessionSocket.send({
      type: "chat",
      message: text,
      session_id: sessionId,
      // Day137b-1：模式切换器（后端 resolve_initial_mode；plan=只读规划）
      mode: ui.chatMode,
      // 新会话标题由后端创建时一次写入（无 PATCH 竞态；列表刷新后标题不丢）
      ...(title ? { title } : {}),
      ...(activeProjectId ? { project_id: activeProjectId } : {}),
      // 员工单聊：session 元数据优先，新会话由本帧携带 target_role 落库
      ...(dmTargetRole
        ? {
            target_role: dmTargetRole,
            ...(dmEmployee?.employee_id ? { employee_id: dmEmployee.employee_id } : {}),
            ...(dmEmployee?.name ? { employee_name: dmEmployee.name } : {}),
            session_origin: "employee_dm",
          }
        : {}),
      // 秘书入口发起的会话带来源标记（面板秘书列表数据源）；
      // 空态（英雄区）首条消息且已选项目 → 同样按项目秘书会话
      // （不进项目群聊；与项目面板内「秘书新建会话」同口径）
      ...(!dmTargetRole && pendingOrigin
        ? { session_origin: pendingOrigin }
        : !dmTargetRole && isNewSession && activeProjectId
          ? { session_origin: "secretary" }
          : {}),
      // Day137b-2：附件引用（upload_id）+ 会话级模型/思考覆盖（控制条）
      ...(attachments?.length
        ? {
            attachments: attachments.map((item) => ({
              upload_id: item.upload_id,
              name: item.name,
            })),
          }
        : {}),
      ...(ui.sessionModel ? { model: ui.sessionModel } : {}),
      ...(ui.sessionThinking ? { thinking_mode: ui.sessionThinking } : {}),
    });
    // 团队会话派单已由后端接管：项目有团队时本 chat 帧统一走公司路由
    // （无 @ 由顶部角色/内容路由承接；员工产出经线程回写本会话），不再重复发派单帧
    // 来源标记只在建会话时生效一次（防污染后续消息）
    store.setPendingSessionOrigin("");
    setPendingEmployee(null);
  };

  // 中断重试（2026-10-15 用户拍板）：重试属于**新的一次请求**——先终止内核侧
  // 可能仍挂着的旧轮（刷新页面不会终止它，槽位未释放时重发会撞 SESSION_BUSY），
  // 再以同内容重发；中断上下文（目标+已执行过程）经会话历史自动携带。
  const retryInterrupted = async (messageId: string) => {
    if (activeSessionId) {
      try {
        await sessionsApi.abort(activeSessionId);
      } catch {
        // 404 = 内核侧旧轮已不在（槽位已释放）；其他错误同样不阻断重试
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
    resend(messageId);
  };

  const resend = (messageId: string) => {
    // 忙时禁止重发：send() 会抢占流式槽并开第二个 turn（被后端 SESSION_BUSY 拒）
    // 会把进行中的回复截断且无提示（复核修复；按钮侧同步禁用）
    if (busy) return;
    const target = messages.find((item) => item.id === messageId);
    if (!target || target.role !== "user") return;
    send(target.content);
  };

  // 编辑已发送消息——从此处重新开始对话
  // （截断该消息及之后的对话，已有产物/工作区文件不动，用编辑后内容重发）
  const editResend = async (messageId: string, text: string): Promise<boolean> => {
    if (!activeSessionId || !text.trim()) return false;
    // 忙时保持编辑态并提示（MessageItem 依据 false 不关闭编辑框，防静默丢草稿）
    if (busy) {
      toast.error(t("chat.message.editBusy"));
      return false;
    }
    const index = messages.findIndex((item) => item.id === messageId);
    if (index < 0) return false;
    // 恢复点 = 该消息之前最近的后端 entry；无前置（编辑首条）→ 空串 = 截断全部
    const previousEntryId = [...messages.slice(0, index)]
      .reverse()
      .find((item) => item.entryId)?.entryId;
    try {
      await sessionsApi.revertStage(activeSessionId, previousEntryId ?? "");
      await sessionsApi.revertCommit(activeSessionId);
      const detail = await sessionsApi.detail(activeSessionId);
      useChatStore
        .getState()
        .loadMessages(activeSessionId, entriesToMessages(activeSessionId, detail.messages));
      send(text);
      return true;
    } catch (error) {
      toast.error(errorMessage(error));
      return false;
    }
  };

  // 重新生成：服务端保留该轮 user 消息与附件，只重跑 assistant 回复
  const regenerate = async (messageId: string) => {
    if (busy || !activeSessionId) return;
    const message = messages.find((item) => item.id === messageId);
    if (!message?.entryId) return;
    try {
      await sessionsApi.regenerate(activeSessionId, message.entryId);
      const detail = await sessionsApi.detail(activeSessionId);
      useChatStore
        .getState()
        .loadMessages(activeSessionId, entriesToMessages(activeSessionId, detail.messages));
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  // -3b：失败工具节点级操作——重试（force 真跑，结果只回填前端内存节点）/ 本地跳过
  const retryTool = async (toolCallId: string) => {
    if (!activeSessionId) return;
    try {
      const result = await sessionsApi.retryTool(activeSessionId, toolCallId);
      useChatStore.getState().applyToolRetry(activeSessionId, toolCallId, {
        success: result.success,
        outputPreview: result.output_preview,
        durationMs: result.duration_ms,
        error: result.error,
      });
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const skipTool = (nodeId: string) => {
    if (!activeSessionId) return;
    // 找到节点携带的 toolCallId（跳过态随存的定位键；旧帧无 id 只能本地跳过）
    const list = useChatStore.getState().messagesBySession[activeSessionId] ?? [];
    let toolCallId: string | undefined;
    for (const item of list) {
      for (const node of item.process ?? []) {
        if (node.kind === "tool" && node.id === nodeId) toolCallId = node.toolCallId;
      }
    }
    useChatStore.getState().skipToolNode(activeSessionId, nodeId);
    if (toolCallId) {
      // best-effort 随存：失败不提示（本地跳过态已生效）
      sessionsApi.processSkip(activeSessionId, toolCallId).catch(() => undefined);
    }
  };

  // -3b：重发本轮——失败工具所在轮的 user 输入重新发送（复用 resend 忙时守卫）
  const resendTurnBefore = (assistantMessageId: string) => {
    const index = messages.findIndex((item) => item.id === assistantMessageId);
    if (index < 0) return;
    const target = [...messages.slice(0, index)].reverse().find((item) => item.role === "user");
    if (target) resend(target.id);
  };

  const steer = (text: string) => {
    if (!activeSessionId) return;
    sessionsApi
      .steer(activeSessionId, text)
      .then(() => {
        useChatStore.getState().appendSystemNote(activeSessionId, t("chat.note.steered"));
      })
      .catch((error) => toast.error(errorMessage(error)));
  };

  const stop = () => {
    if (!activeSessionId) return;
    // 用户主动终止：立即标记终态 USER_INTERRUPT（tier=none）——不得出现
    // 「重试本轮」横幅（主动停止 ≠ 异常中断）
    useChatStore.getState().markSessionTerminal(activeSessionId, "USER_INTERRUPT");
    sessionsApi
      .abort(activeSessionId)
      .then(() => {
        // 后端已受理中断：正常路径等 done(INTERRUPT) 帧定稿；超时未见终结帧 → 本地兜底
        // （防后端吞帧/长工具收尾期间的「正在回复中」残留）
        window.setTimeout(() => {
          const state = useChatStore.getState();
          if (state.streams[activeSessionId]) {
            state.abortLocalStream(activeSessionId);
          }
          // 员工过程轨同步收尾（防中断后再发消息时「正在执行」与「正在回复中」
          // 双态并存——中断兜底补清过程轨）
          const settled = state.settleStaleEmployeeProgress();
          for (const key of settled) {
            window.setTimeout(() => {
              useChatStore.getState().expireEmployeeProgressByKey(key);
            }, 2500);
          }
        }, ABORT_SETTLE_WINDOW_MS);
      })
      .catch((error) => {
        // 404 NO_ACTIVE_ABORT_CONTROLLER = 后端该会话无活动流（前端槽孤立）→
        // 本地兜底定稿，防「正在回复中」永久残留
        if (error instanceof ApiError && error.status === 404) {
          useChatStore.getState().abortLocalStream(activeSessionId);
          return;
        }
        toast.error(errorMessage(error));
      });
  };

  const stageRevert = async (entryId: string) => {
    if (!activeSessionId) return;
    try {
      await sessionsApi.revertStage(activeSessionId, entryId);
      useChatStore.getState().setStagedRevert({ sessionId: activeSessionId, entryId });
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const undoRevert = async (sessionId: string) => {
    try {
      const result = await sessionsApi.revertUndo(sessionId);
      if (result.restored_entries <= 0) {
        toast.info(t("chat.toast.undoEmpty"));
        return;
      }
      const detail = await sessionsApi.detail(sessionId);
      useChatStore
        .getState()
        .loadMessages(sessionId, entriesToMessages(sessionId, detail.messages));
      toast.success(t("chat.toast.undoDone"));
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const commitRevert = async () => {
    if (!stagedRevert) return;
    const sessionId = stagedRevert.sessionId;
    try {
      await sessionsApi.revertCommit(sessionId);
      useChatStore.getState().setStagedRevert(null);
      const detail = await sessionsApi.detail(sessionId);
      useChatStore
        .getState()
        .loadMessages(sessionId, entriesToMessages(sessionId, detail.messages));
      toast.success(t("chat.toast.reverted"), {
        action: {
          label: t("chat.toast.undo"),
          onClick: () => void undoRevert(sessionId),
        },
      });
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const clearRevert = async () => {
    if (!stagedRevert) return;
    try {
      await sessionsApi.revertClear(stagedRevert.sessionId);
      useChatStore.getState().setStagedRevert(null);
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  const last = messages[messages.length - 1];
  // 中断提示按**终态分档**：模型不可用 / 超时 / 安全拦截 / 上下文超长各给可行动文案，
  // 不再一律显示「因网络或模型原因中断了」（后端已给出明确原因，前端原先丢弃）
  const resumeKey = last ? `${sessionKey}:${last.id}` : "";
  const terminalReason = useChatStore((state) => state.sessionTerminals[sessionKey] ?? "");
  const tier = interruptionTier(terminalReason);
  const interrupted =
    !busy &&
    last !== undefined &&
    last.done &&
    last.role === "user" &&
    tier !== "none" &&
    resumeKey !== dismissedResume;
  // 多张决策卡并存时**允许上下切换查看**（此前只显示第一张
  // 用户对第一题没想好就被挡住；现在可先翻看/跳到别的决策点，回头再答第一张）。
  const pendingInteractions = useMemo(
    () =>
      Object.values(interactions).filter(
        (frame) =>
          frame.session_id === sessionKey ||
          // 员工 headless 审批（approval_store 来源）任何会话可见可决策
          frame.interaction_id.startsWith(APPROVAL_FRAME_PREFIX),
      ),
    [interactions, sessionKey],
  );
  const [interactionIndex, setInteractionIndex] = useState(0);
  const pendingInteraction =
    pendingInteractions[Math.min(interactionIndex, pendingInteractions.length - 1)];
  // §18.16①：作答草稿按 interaction_id 分桶持久化。key={interaction_id} 让切卡时
  // 卡片重挂载（-3 要求「卡 A 移除后卡 B 不残留 A 的答案」），但重挂载也会连带丢掉
  // 用户在 A 上已答的内容——而 §18.16① 的语义恰恰是「先翻到别的决策点，回头再答这张」。
  // 分桶后两个语义同时成立：新卡无桶即全新，同卡回访从桶复原；卡片解决时删桶防泄漏。
  const [cardDrafts, setCardDrafts] = useState<Record<string, InteractionDraft>>({});
  // 当前卡被解决/移除后游标回落到有效范围（否则会停在空档）
  useEffect(() => {
    if (interactionIndex > pendingInteractions.length - 1) {
      setInteractionIndex(Math.max(0, pendingInteractions.length - 1));
    }
  }, [interactionIndex, pendingInteractions.length]);
  // 项目空间卡片仅在「新建会话且尚未开始对话」时出现
  // （一旦进入任务/已有消息即隐藏；有项目归属时卡片显示项目名称）
  // 空态判定：仅「没有会话」的全新会话算新会话；已有会话（如刚建的空群聊）
  // 消息为空也必须走其原路径——否则发送会被强制标成秘书会话
  // （新建群聊首条消息需走普通助理路径，不带公司卡）。
  const isNewSession = !activeSessionId && messages.length === 0 && !busy;
  useEffect(() => {
    if (!isNewSession) setPendingEmployee(null);
  }, [isNewSession]);
  // 空态展示（任意会话：新建或已有但无消息——如刚建的空群聊）统一英雄布局：
  // 品牌 + 输入框居中，与普通会话空态一致；语义分类（发送归属等）仍走 isNewSession
  const emptyChat = !busy && messages.length === 0;
  // 空态英雄区 = 品牌 + 输入框居中（挂起交互卡出现时保持贴底，不遮决策）
  const heroEmpty = emptyChat && !pendingInteraction;
  // 逻辑：项目已有团队（成员）时不显示「还没有团队与员工」提示；无项目归属回落全局员工判定
  const showSecretaryHint =
    isNewSession && (activeProjectId ? projectHasTeam === false : loaded && employees.length === 0);
  // 秘书身份：本项目不存在「AI 助手」——全能助手即秘书；
  // 一切无员工署名的 assistant 回复身份头 =「秘书」（员工线程回复 senderName 优先显示员工名）；
  // 员工会话（role-session::…::<role_id>）身份头 = 对应员工档案（名字 + 按 role 稳定头像色）
  const sessionRoleId = useMemo(() => {
    const parts = String(sessionKey || "").split("::");
    if (parts.length < 2 || parts[0] !== "role-session") return "";
    return parts[parts.length - 1] || "";
  }, [sessionKey]);
  const sessionEmployee = sessionRoleId
    ? employees.find((employee) => employee.role_id === sessionRoleId)
    : undefined;
  const assistantIdentity: AssistantIdentity | null = sessionEmployee
    ? { name: sessionEmployee.name, roleId: sessionEmployee.role_id }
    : projectHasTeam && !isSecretarySession
      ? // 团队会话无署名流式占位：不显示「秘书」身份头（团队里没有这个角色）
        null
      : {
          name: t("chat.secretary.name"),
          roleId: SECRETARY_ROLE_ID,
        };

  return (
    <div className="bg-chat-bg flex h-full min-h-0" data-testid="chat-view">
      {/* 137b-3d：右栏收起时仍保留展开入口；主列承担全部原有纵向布局 */}
      <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
        {stagedRevert && stagedRevert.sessionId === sessionKey ? (
          <RevertBanner
            entryId={stagedRevert.entryId}
            onCommit={() => void commitRevert()}
            onClear={() => void clearRevert()}
          />
        ) : null}
        {/* 2026-09-23 用户拍板：会话内搜索/历史提问已移至顶栏（AppHeader 渲染，与连接状态同排） */}

        {activeRound ? <GroupRoundBar round={activeRound} /> : null}

        {messages.length ? (
          <MessageList
            messages={messages}
            sessionKey={sessionKey}
            onRevertStage={(entryId) => void stageRevert(entryId)}
            onEditResend={(messageId, text) => editResend(messageId, text)}
            onRetryTool={(toolCallId) => void retryTool(toolCallId)}
            onSkipTool={skipTool}
            onResendTurn={(messageId) => resendTurnBefore(messageId)}
            onRegenerate={(messageId) => void regenerate(messageId)}
            // v5 //工具节点 → 详情页签；附件 chip → 预览页签；@提及 → 员工视图
            onToolDetail={(messageId, nodeId) =>
              usePanelStore.getState().focusTool(messageId, nodeId)
            }
            onAttachmentOpen={(uploadId, name) =>
              usePanelStore.getState().focusAttachment(uploadId, name)
            }
            onMentionClick={(name) => {
              const employee = findEmployeeByName(employees, name);
              if (employee) usePanelStore.getState().focusEmployee(employee.role_id);
            }}
            onFeedback={(messageId, value) => {
              void useChatStore
                .getState()
                .setFeedback(sessionKey, messageId, value)
                .catch((reason: unknown) =>
                  toast.error(reason instanceof Error ? reason.message : String(reason)),
                );
            }}
            mentions={mentionNames}
            assistantIdentity={assistantIdentity}
            memberTasks={memberTasks}
            replyArtifacts={replyArtifacts}
            onArtifactOpen={(artifactId, name, path) =>
              usePanelStore.getState().focusArtifact(artifactId, name, path)
            }
          >
            {pendingReplyEmployee ? (
              <PendingEmployeeReply
                roleId={pendingReplyEmployee.roleId}
                name={pendingReplyEmployee.name}
                metadata={pendingReplyEmployee.metadata}
              />
            ) : null}
            {/* 员工执行的实时过程随消息流滚动，不固定挂在消息区下方挤占空间；
                秘书会话不承载员工工作流——不渲染 */}
            {isSecretarySession || !activeProjectId ? null : (
              <LiveProgressList projectId={activeProjectId} />
            )}
          </MessageList>
        ) : null}

        {/* 员工执行的实时过程已挪入消息流尾部（MessageList children——随滚动，
            不再固定挤占消息区）；此处原独立渲染块删除 */}

        {/* 2026-09-29 用户拍板：新建会话英雄区——品牌字段 + 输入框组垂直居中
            （不再显示秘书头像；秘书提示改挂在输入框下方 below 槽） */}
        <div
          className={cn(
            "flex min-h-0 min-w-0 flex-col",
            emptyChat && "flex-1",
            emptyChat && (heroEmpty ? "justify-center" : "justify-end"),
          )}
          data-testid={isNewSession ? "chat-empty" : "composer-dock"}
        >
          {heroEmpty ? (
            <div
              className="flex items-end justify-center pb-3"
              style={{ containerType: "inline-size", gap: "clamp(8px, 1.8cqw, 14px)" }}
            >
              <img
                src={`${import.meta.env.BASE_URL}logo.svg`}
                alt=""
                className="opacity-[0.08]"
                style={{
                  width: "clamp(32px, 8cqw, 64px)",
                  height: "clamp(32px, 8cqw, 64px)",
                }}
              />
              <span
                style={{
                  fontFamily: "Eavm",
                  fontSize: "clamp(28px, 7.5cqw, 60px)",
                  transform: "translateY(clamp(4px, 1.4cqw, 11px))",
                }}
                className="text-divider leading-none font-semibold tracking-tight"
              >
                {t("app.name")}
              </span>
            </div>
          ) : null}
          <Composer
            busy={busy}
            disabled={projectLocked}
            teamRunning={teamRunning}
            enhanced
            projectId={activeProjectId}
            allowTeamPicker={projectHasGroupChat}
            mentionRoleIds={projectHasGroupChat ? projectMemberRoles : undefined}
            pendingEmployee={isNewSession ? composerEmployee : null}
            onPickEmployee={() => setEmployeePickerOpen(true)}
            onClearEmployee={() => setPendingEmployee(null)}
            allowEmployeePicker={isNewSession && !activeSessionTargetRole}
            onSend={send}
            onSteer={steer}
            onStop={() => {
              if (busy) stop();
              if (teamRunning) void stopTeam();
            }}
            above={
              interrupted || pendingInteraction ? (
                <div className="flex flex-col gap-2">
                  {interrupted ? (
                    <div
                      className="flex items-center gap-2 rounded-xl px-3 py-2 text-xs"
                      data-testid="resume-banner"
                    >
                      <CircleAlert className="text-destructive size-3.5 shrink-0" />
                      <span className="text-chat-ink flex-1">{t(interruptionHintKey(tier))}</span>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-6 px-2 text-xs"
                        data-testid="resume-cancel"
                        onClick={() => {
                          setDismissedResume(resumeKey);
                          useChatStore.getState().markSessionTerminal(sessionKey, "USER_INTERRUPT");
                        }}
                      >
                        {t("chat.resume.cancel")}
                      </Button>
                      {tierAllowsRetry(tier) ? (
                        <Button
                          size="sm"
                          className="bg-chat-ink hover:bg-chat-ink/90 h-6 px-2 text-xs text-white"
                          data-testid="resume-retry"
                          onClick={() => {
                            if (last) void retryInterrupted(last.id);
                          }}
                        >
                          {t("chat.resume.action")}
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                  {pendingInteraction ? (
                    <div className="flex flex-col gap-1" data-testid="interaction-stack">
                      {/* 2026-10-01 用户拍板：卡序指示从卡外（本处原「待决策 N 项 · 第 N 项」条）
                          挪进 InteractionCard 头部（倒计时左侧，形如 1/2） */}
                      <InteractionCard
                        key={pendingInteraction.interaction_id}
                        frame={pendingInteraction}
                        initialDraft={cardDrafts[pendingInteraction.interaction_id]}
                        cardIndex={interactionIndex}
                        cardTotal={pendingInteractions.length}
                        onPrevCard={() => setInteractionIndex((index) => Math.max(0, index - 1))}
                        onNextCard={() =>
                          setInteractionIndex((index) =>
                            Math.min(pendingInteractions.length - 1, index + 1),
                          )
                        }
                        onDraftChange={(id, draft) =>
                          setCardDrafts((previous) => ({ ...previous, [id]: draft }))
                        }
                        onResolved={(id) => {
                          setCardDrafts((previous) => {
                            if (!(id in previous)) return previous;
                            const next = { ...previous };
                            delete next[id];
                            return next;
                          });
                          useChatStore.getState().resolveInteraction(id);
                        }}
                        onError={(message) => toast.error(message)}
                      />
                    </div>
                  ) : null}
                </div>
              ) : undefined
            }
            aboveTone={
              // 底色贯通：重试横幅在场 → 整壳取浅红主题（横幅透明融入，
              // 不再是「红条叠紫底」）；其余（含空态/仅卡片）取中性紫。决策卡
              // 自持白底，在任一壳色上均清晰、不被染色。
              interrupted ? "bg-destructive/10" : "bg-primary/5"
            }
            below={isNewSession ? <ProjectSpaceCard /> : undefined}
          />
          {/* 2026-09-29 用户拍板：秘书提示在「整个会话框 + 项目空间卡」之外（壳外、面板居中）；
            第五轮：与会话框/卡片间距再收（负 margin 抵消 Composer 组件底部留白 pb-5） */}
          {showSecretaryHint ? (
            <p
              className="text-muted-foreground-weak -mt-3 text-center text-xs"
              data-testid="chat-secretary-hint"
            >
              {t("chat.secretary.chatHint")}
            </p>
          ) : null}
        </div>
      </div>
      <EmployeePickerDialog
        open={employeePickerOpen}
        onOpenChange={setEmployeePickerOpen}
        employees={employeeOptions}
        onSelect={(employee) => setPendingEmployee(employee)}
      />
    </div>
  );
}
