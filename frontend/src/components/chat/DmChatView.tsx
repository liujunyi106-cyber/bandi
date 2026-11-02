/**
 * 功能：员工单聊视图（@base docs/前端设计规划.md §6 / ——Day137 完整增强）。
 * 对话容器 = 与目标员工的讨论线程（participants=[user, target]）：
 * 历史/实时经线程通道（GET threads + thread_message 帧），发送经 /ws/company target_role
 * （触发员工执行，产出经 回写路由流入同一线程）。
 * 增强：员工名/头像 + 任务状态徽标（work-items 查询）+ 空员工引导。
 * ：会话内搜索/历史提问移到顶栏（AppHeader），本视图只发布数据。
 */
import { ArrowDown, ListChecks, Loader2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Composer } from "@/components/chat/Composer";
import { EmptyEmployeeState } from "@/components/chat/EmptyEmployeeState";
import { InteractionCard } from "@/components/chat/InteractionCard";
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import { ProcessBlock } from "@/components/chat/ProcessBlock";
import { ReplySources } from "@/components/chat/ReplySources";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { APPROVAL_FRAME_PREFIX, useCompanyApprovals } from "@/hooks/use-company-approvals";
import { useEmployees } from "@/hooks/use-employees";
import { toast } from "sonner";
import { useT } from "@/i18n";
import { resolveEmployeeName } from "@/lib/employee";
import { shouldUseDocumentLayout } from "@/lib/markdown";
import { handleMentionClick } from "@/lib/panel-link";
import { normalizeProjectScope } from "@/lib/progress-key";
import { cn } from "@/lib/utils";
import { companyApi, findDirectThread, type ThreadMessageRecord } from "@/services/company";
import { socket } from "@/services/ws";
import {
  nodesFromEvents,
  settleRunningNodes,
  useChatStore,
  type ChatAttachment,
  type ProcessNode,
} from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

/** 稳定空引用（zustand selector 返回值必须引用稳定，否则触发无限重渲）。 */
const EMPTY_PROGRESS: ProcessNode[] = [];
/** 真正处于执行中的 phase（needs_attention 的卡在 in_progress 列但并未执行）。 */
const ACTIVE_EXECUTION_PHASES = new Set(["running", "claimed", "executing"]);

/** 功能：历史过程快照结算——消息已到达即该轮结束；历史事件无真实时间戳，
 *  去掉回放瞬间生成的 `startedAt`，避免显示「已完成 · 1ms」这类假耗时。 */
function settleHistoryProcess(events: unknown[]): ProcessNode[] {
  return settleRunningNodes(
    nodesFromEvents(events).map((node) => ({
      ...node,
      startedAt: undefined,
      durationMs: undefined,
    })),
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 单聊消息（服务端线程消息 + 本地过程固化——回复到达时把过程槽附加到该消息）。 */
type DmMessage = ThreadMessageRecord & { process?: ProcessNode[]; durationMs?: number };

interface DmChatViewProps {
  targetRole: string;
  /** 项目上下文（per-project 线程空间；空 = company 空间）。 */
  projectId?: string;
}

export function DmChatView({ targetRole, projectId = "" }: DmChatViewProps) {
  const t = useT();
  const { employees, loaded } = useEmployees();
  useCompanyApprovals();
  const interactions = useChatStore((state) => state.interactions);
  const pendingInteraction = useMemo(
    () =>
      Object.values(interactions).find(
        (frame) =>
          (frame.interaction_id.startsWith(APPROVAL_FRAME_PREFIX) ||
            frame.kind === "input" ||
            frame.kind === "permission") &&
          String(frame.session_id).includes(targetRole),
      ),
    [interactions, targetRole],
  );
  // 员工独立模型（换大脑）——llm_config.default_model 存在即该员工
  // 用自己的模型，不随全局激活档案变化；控制条据此显示并锁定切换。
  const employeeOwnModel = useMemo(() => {
    const match = employees.find(
      (item) => item.role_id === targetRole || item.employee_id === targetRole,
    );
    return match?.llm_config?.default_model ?? "";
  }, [employees, targetRole]);
  // @提及高亮名单（员工名 + 角色 id）——三视图同口径；此前单聊漏传 mentions
  // 致「@员工」在单聊里既不高亮也不可点（「发送信息应显示蓝色」）。
  const mentionNames = useMemo(
    () => employees.flatMap((employee) => [employee.name, employee.role_id]),
    [employees],
  );
  const [threadId, setThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<DmMessage[]>([]);
  const [activeCount, setActiveCount] = useState<number | null>(null);
  // 本轮是否已发出、尚未收到员工回复——回复落位即结束（不依赖工作项卡终态滞后）
  const [awaitingReply, setAwaitingReply] = useState(false);
  const [retiredName, setRetiredName] = useState("");
  const [retiredLocked, setRetiredLocked] = useState(false);
  const retired = retiredName !== "";
  // 前端入口：引用已通过任务 → 下一条消息走修正通道
  const [reworkCards, setReworkCards] = useState<{ id: string; title: string }[]>([]);
  const [reworkPick, setReworkPick] = useState<{ id: string; title: string } | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const threadIdRef = useRef<string | null>(null);
  // 本地发送时刻：服务端 user 回写帧可能不带 created_at（用收到时间会导致
  // 本轮耗时只算「收到帧 → 回复」的几百毫秒），无时间戳时回落到发送时刻
  const lastSentAtRef = useRef<string | null>(null);
  const lastSentAtMsRef = useRef<number | null>(null);
  // 回复落位时刻：早于该时刻创建的 running 卡不再计入顶部「执行中」。
  const lastReplyAtRef = useRef(0);
  // 回到底部按钮（与群聊 MessageList 同款复用）
  const dmScrollRef = useRef<HTMLDivElement>(null);
  const dmStickRef = useRef(true);
  const [dmAtBottom, setDmAtBottom] = useState(true);

  const handleDmScroll = useCallback(() => {
    const element = dmScrollRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    const stick = distance <= 80;
    dmStickRef.current = stick;
    setDmAtBottom(stick);
  }, []);

  // 新消息/过程更新贴底（用户上滚时不打扰）
  useEffect(() => {
    if (!dmStickRef.current) return;
    const element = dmScrollRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [messages]);

  useEffect(() => {
    threadIdRef.current = threadId;
  }, [threadId]);

  // 会话内搜索/历史提问发布到顶栏槽位（AppHeader 渲染）
  const toolItems = useMemo(
    () =>
      messages.map((message) => ({
        id: message.message_id,
        role: message.sender === "user" ? "user" : message.sender,
        content: message.content,
      })),
    [messages],
  );
  useEffect(() => {
    useUiStore.getState().setHeaderTools(toolItems, "dm-tools");
    return () => useUiStore.getState().clearHeaderTools();
  }, [toolItems]);

  // 插队批：员工执行过程实时块（thread_progress 帧归并——不依赖 work-items 轮询）
  // 只认**本单聊线程**的槽（线程未建立时匹配空线程键）；不跨线程兜底——
  // 否则群聊/其他会话的执行过程会串进单聊视图（「回复完了还在执行」的串台源）。
  const progressNodes = useChatStore((state) => {
    const key = `${normalizeProjectScope(projectId)}::${threadId ?? ""}::${targetRole}`;
    return state.employeeProgress[key] ?? EMPTY_PROGRESS;
  });
  const progressText = useChatStore((state) => {
    const key = `${normalizeProjectScope(projectId)}::${threadId ?? ""}::${targetRole}`;
    return state.employeeProgressText[key] ?? "";
  });

  const refreshTasks = useCallback(() => {
    if (!targetRole) return;
    void companyApi
      .workItems(targetRole)
      .then(({ items }) => {
        // 只统计本单聊线程的进行中卡：群聊/其他会话的卡不得计入顶部「执行中」
        const scoped = threadId
          ? items.filter((item) => {
              if (!ACTIVE_EXECUTION_PHASES.has(item.phase) || item.thread_id !== threadId)
                return false;
              const createdAt = item.created_at
                ? Date.parse(item.created_at.replace(" ", "T"))
                : Number.NaN;
              return !(
                lastReplyAtRef.current > 0 &&
                Number.isFinite(createdAt) &&
                createdAt <= lastReplyAtRef.current
              );
            })
          : [];
        setActiveCount(scoped.length);
        // 可修正卡 = 已通过——与后端受控回转的 fail-closed 口径一致；
        // 会话卡（chat_*）是对话记录不是任务，不进可修正清单
        setReworkCards(
          items
            .filter(
              (item) =>
                item.phase === "approved" &&
                item.source !== "chat_mention" &&
                item.source !== "chat_dm",
            )
            .map((item) => ({ id: item.id, title: item.title || item.id.slice(0, 8) })),
        );
      })
      .catch(() => undefined);
  }, [targetRole, threadId]);

  useEffect(() => {
    refreshTasks();
  }, [refreshTasks]);

  // 兜底：发送后长时间无回复（断流/限流）也要解除执行态，避免永久「正在执行」
  useEffect(() => {
    if (!awaitingReply) return;
    const timer = window.setTimeout(() => setAwaitingReply(false), 180_000);
    return () => window.clearTimeout(timer);
  }, [awaitingReply]);

  useEffect(() => {
    if (!targetRole) return;
    let cancelled = false;
    setThreadId(null);
    setMessages([]);
    setRetiredName("");
    setRetiredLocked(false);
    setReworkPick(null);
    setPickerOpen(false);
    void companyApi.employeeTombstone(targetRole).then((result) => {
      if (!cancelled && result) setRetiredName(result.tombstone.name || targetRole);
    });
    void companyApi
      .threads(projectId)
      .then(async ({ threads }) => {
        const found = findDirectThread(threads, targetRole);
        if (!found || cancelled) return;
        setThreadId(found.thread_id);
        const detail = await companyApi.thread(found.thread_id, projectId);
        if (cancelled) return;
        // 历史消息带回写时持久化的过程快照——刷新后过程块可回看
        setMessages(
          detail.messages.map((message) => {
            const events = Array.isArray(message.process_events) ? message.process_events : [];
            return events.length > 0
              ? { ...message, process: settleHistoryProcess(events) }
              : message;
          }),
        );
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [targetRole, projectId]);

  useEffect(() => {
    if (
      retired &&
      messages.some(
        (message) =>
          message.sender === "system" && message.content === t("chat.note.employeeRetired"),
      )
    ) {
      setRetiredLocked(true);
    }
  }, [messages, retired, t]);

  useEffect(() => {
    if (!targetRole) return;
    return socket.on("thread_message", (frame) => {
      const data =
        frame.data && typeof frame.data === "object" ? (frame.data as Record<string, unknown>) : {};
      // 检查修复：项目过滤（company 空间映射 "company"）
      // 两侧统一走 normalizeProjectScope——原「帧侧 ?? "company"」与
      // 「视图侧 || "company"」是两套写法，空 project 帧会与写侧键错开
      const frameProject = normalizeProjectScope(String(data.project_id ?? ""));
      if (frameProject !== normalizeProjectScope(projectId)) return;
      // 参与方集合精确判定直聊线程（[user, target] 且恰两人）——群聊线程（无 user）
      // 与其他员工直聊线程不注入本视图；线程未加载完成（threadId 未知）时同样安全
      const participants = Array.isArray(data.participants) ? (data.participants as string[]) : [];
      const isDirect =
        participants.length === 2 &&
        participants.includes("user") &&
        participants.includes(targetRole);
      if (!isDirect) return;
      // 已确定线程时按 thread_id 过滤（防其他线程串入本单聊视图）
      const incoming = String(data.thread_id ?? "");
      const current = threadIdRef.current;
      if (current && incoming && incoming !== current) return;
      // 首条消息前线程 id 尚未落地：用服务端帧里的线程 id 补位，
      // 否则实时过程槽（按键含 thread）对不上本视图的槽键。
      if (!current && incoming) setThreadId(incoming);
      // 员工执行过程随回复固化——**先消费槽再 setState**
      // 在 updater 内消费会因 React 重放 updater 导致第二次取到空槽、过程块丢失）
      // 槽键含 thread（当时用于过滤的同一 thread_id ⇒ 过程与回复精确配对）
      const processNodes = useChatStore
        .getState()
        .consumeEmployeeProgress(normalizeProjectScope(projectId), incoming, targetRole);
      // 实时槽为空时回落帧内过程快照（页面在回写瞬间打开等错过过程帧的场景）
      const frameEvents = Array.isArray(data.process_events)
        ? (data.process_events as unknown[])
        : [];
      // 固化收尾：回写帧是执行中途快照（尾无 done）——落定即结算 running（死状态）
      const process = settleRunningNodes(
        processNodes.length > 0 ? processNodes : nodesFromEvents(frameEvents),
      );
      const sender = String(data.sender ?? "");
      const createdAt = data.created_at
        ? String(data.created_at)
        : sender === "user" && lastSentAtRef.current
          ? lastSentAtRef.current
          : new Date().toISOString();
      // 本轮耗时用同一前端时钟（发送时刻 → 收到回复），不依赖服务端时间戳
      const durationMs =
        sender !== "user" && lastSentAtMsRef.current !== null
          ? Math.max(0, Date.now() - lastSentAtMsRef.current)
          : undefined;
      setMessages((previous) => [
        ...previous,
        {
          sender,
          content: String(data.content ?? ""),
          mentions: Array.isArray(data.mentions) ? (data.mentions as string[]) : [],
          created_at: createdAt,
          message_id: String(data.message_id ?? ""),
          process,
          ...(durationMs !== undefined ? { durationMs } : {}),
        },
      ]);
      // 员工回复已到达 ⇒ 本轮执行对用户而言结束：立即清「执行中」徽标/静态提示
      // （卡的终态与 gate 收敛滞后数秒，不得残留成「回复完了还在执行」）；
      // 稍后再拉一次校正——同线程若确有下一张卡在跑，会重新显示。
      if (String(data.sender ?? "") !== "user") {
        lastReplyAtRef.current = Date.now();
        setActiveCount(0);
        setAwaitingReply(false);
        window.setTimeout(refreshTasks, 2500);
      } else {
        refreshTasks();
      }
    });
  }, [targetRole, projectId, refreshTasks]);

  // 发送后的 ok 帧带回线程（含 participants 校验，防串线程）
  useEffect(() => {
    if (!targetRole) return;
    return socket.on("ok", (frame) => {
      // 检查修复：ok 帧同样按项目过滤（防其他项目发送回执污染本视图）
      if (
        normalizeProjectScope(String(frame.project_id ?? "")) !== normalizeProjectScope(projectId)
      )
        return;
      const thread = frame.thread as Record<string, unknown> | null | undefined;
      if (!thread || typeof thread.thread_id !== "string") return;
      const participants = Array.isArray(thread.participants)
        ? (thread.participants as string[])
        : [];
      if (participants.includes(targetRole) && participants.includes("user")) {
        setThreadId(thread.thread_id);
      }
    });
  }, [targetRole, projectId]);

  const send = (text: string, attachments?: ChatAttachment[]) => {
    const value = text.trim();
    if ((!value && !attachments?.length) || !targetRole) return;
    if (retired) {
      if (!threadId) {
        setRetiredLocked(true);
        return;
      }
      void companyApi
        .appendThreadSystemNote(
          threadId,
          t("chat.note.employeeRetired"),
          "employee_retired",
          projectId,
          `employee_retired:${targetRole}`,
        )
        .then(() => companyApi.thread(threadId, projectId))
        .then((detail) => {
          setMessages(
            detail.messages.map((message) => {
              const events = Array.isArray(message.process_events) ? message.process_events : [];
              return events.length > 0
                ? { ...message, process: settleHistoryProcess(events) }
                : message;
            }),
          );
          setRetiredLocked(true);
        })
        .catch((error) => toast.error(errorMessage(error)));
      return;
    }
    // 项目上下文透传——线程归入项目空间（per-project 工厂）；
    // 引用已通过任务时带 target_work_item_id → 后端受控回转
    // 轮三-5：对话附件（upload_id）→ 服务端文本注记拼入建卡内容（员工执行时可见）
    socket.send({
      content: value,
      sender_role: "user",
      target_role: targetRole,
      // 单聊模式切换（play/execute）随消息透传——后端建 PLAN/EXECUTE 卡驱动员工
      mode: useUiStore.getState().chatMode,
      ...(projectId ? { project_id: projectId } : {}),
      ...(reworkPick ? { target_work_item_id: reworkPick.id } : {}),
      ...(attachments?.length
        ? {
            attachments: attachments.map((item) => ({
              upload_id: item.upload_id,
              name: item.name,
            })),
          }
        : {}),
    });
    // 引用一次性消费（下一条消息不携带修正语义）
    setReworkPick(null);
    lastSentAtRef.current = new Date().toISOString();
    lastSentAtMsRef.current = Date.now();
    setAwaitingReply(true);
  };

  // 中断失效修复——原来 onStop={noop} + busy={false}
  // 单聊里停止按钮根本不出现。busy 从过程槽派生（实时），中断走 abort 端点。
  const employeeBusy = progressNodes.length > 0 || awaitingReply;
  const stopEmployee = () => {
    void companyApi
      .workItems(targetRole)
      .then(({ items }) => {
        // 只中断本线程的卡——群聊/其他会话的卡不得被单聊的停止按钮命中
        const running = items.find(
          (item) =>
            ACTIVE_EXECUTION_PHASES.has(item.phase) && (!threadId || item.thread_id === threadId),
        );
        if (!running) {
          toast.info(t("chat.dm.noRunningTask"));
          return;
        }
        void companyApi
          .abortWorkItem(running.id)
          .then(() => toast.success(t("chat.dm.interrupted")))
          .catch(() => toast.error(t("chat.dm.interruptFailed")));
      })
      .catch(() => toast.error(t("chat.dm.interruptFailed")));
  };

  if (!targetRole) {
    return (
      <div className="text-chat-muted flex h-full items-center justify-center text-sm">
        {t("chat.dm.pickEmployee")}
      </div>
    );
  }

  if (loaded && employees.length === 0) return <EmptyEmployeeState />;

  const displayName = retiredName || resolveEmployeeName(employees, targetRole);
  const targetEmployee = employees.find(
    (item) => item.role_id === targetRole || item.employee_id === targetRole,
  );

  return (
    <div
      className="bg-chat-bg flex h-full min-h-0 flex-col"
      data-testid="dm-chat-view"
      onClick={(event) => handleMentionClick(event, employees)}
    >
      <div className="border-chat-hairline flex items-center gap-2 border-b px-4 py-2">
        <EmployeeAvatar
          name={displayName}
          metadata={targetEmployee?.metadata}
          className="size-6 rounded-full text-[10px]"
        />
        <span className="text-chat-ink text-sm font-medium">{displayName}</span>
        <span className="text-chat-muted text-xs">· {t("chat.dm.hint")}</span>
        {/* 2026-09-23 MEM-SCOPE-4 前端入口：选择已通过任务 → 对话修正其执行细节 */}
        <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              data-testid="dm-rework-picker"
              className="text-chat-muted hover:text-foreground ml-auto flex size-6 shrink-0 items-center justify-center rounded-md"
              title={t("chat.dm.reworkPick")}
            >
              <ListChecks className="size-3.5" />
            </button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-64 p-1">
            <p className="text-chat-muted px-2 py-1 text-[11px]">{t("chat.dm.reworkPick")}</p>
            {reworkCards.length === 0 ? (
              <p className="text-chat-muted px-2 py-2 text-center text-xs">
                {t("chat.dm.reworkEmpty")}
              </p>
            ) : (
              reworkCards.map((card) => (
                <button
                  key={card.id}
                  type="button"
                  data-testid="dm-rework-option"
                  className="hover:bg-primary/5 flex w-full items-center rounded-md px-2 py-1.5 text-left text-xs"
                  onClick={() => {
                    setReworkPick(card);
                    setPickerOpen(false);
                  }}
                >
                  <span className="min-w-0 flex-1 truncate">{card.title}</span>
                </button>
              ))
            )}
          </PopoverContent>
        </Popover>
        {activeCount !== null ? (
          <span
            data-testid="dm-task-badge"
            className={cn(
              "rounded-full px-2 py-0.5 text-[11px]",
              activeCount > 0 ? "bg-primary/10 text-primary" : "bg-chat-rail text-chat-muted",
            )}
          >
            {activeCount > 0
              ? t("chat.dm.busy").replace("{n}", String(activeCount))
              : t("chat.dm.idle")}
          </span>
        ) : null}
      </div>

      <div className="relative min-h-0 flex-1">
        <div
          ref={dmScrollRef}
          onScroll={handleDmScroll}
          className="h-full overflow-y-auto px-4 py-4"
        >
          <div className="mx-auto w-full max-w-4xl space-y-3">
            {messages.length === 0 ? (
              <p className="text-chat-muted text-center text-sm">{t("chat.dm.empty")}</p>
            ) : (
              messages.map((message, index) => {
                const isUser = message.sender === "user";
                // 与群聊同口径：本轮耗时 = 上一条用户消息 → 本条回复落卡的墙钟差
                let turnDurationMs: number | undefined = message.durationMs;
                if (turnDurationMs === undefined && !isUser && message.created_at) {
                  for (let back = index - 1; back >= 0; back -= 1) {
                    const previous = messages[back];
                    if (previous.sender === "user") {
                      if (previous.created_at) {
                        const delta =
                          Date.parse(message.created_at) - Date.parse(previous.created_at);
                        if (Number.isFinite(delta) && delta > 0) turnDurationMs = delta;
                      }
                      break;
                    }
                  }
                }
                if (message.sender === "system") {
                  return (
                    <div
                      key={`${message.message_id}:${index}`}
                      className="text-chat-muted flex justify-center py-2 text-xs"
                      data-chat-anchor-key={message.message_id}
                    >
                      <span className="bg-chat-rail rounded-full px-3 py-0.5">
                        {message.content}
                      </span>
                    </div>
                  );
                }
                return (
                  <div
                    // 后端建线程时 user 消息 message_id 恒为 "chat"（多轮追加会重复）——
                    // React key 以序号复合防冲突；锚点保留原 id 供定位语义
                    key={`${message.message_id}:${index}`}
                    data-chat-anchor-key={message.message_id}
                    className={cn("flex w-full flex-col", isUser ? "items-end" : "items-start")}
                  >
                    {/* 2026-09-23 用户拍板：用户消息不带「我」标签；员工行 = 头像 + 姓名 + 时间戳 */}
                    {!isUser ? (
                      <span
                        data-testid="dm-message-meta"
                        className="text-chat-muted mb-1 flex items-end gap-1.5 text-sm leading-none"
                      >
                        <EmployeeAvatar
                          name={resolveEmployeeName(employees, message.sender)}
                          metadata={
                            employees.find((item) => item.role_id === message.sender)?.metadata
                          }
                          className="size-6 rounded-full text-xs"
                          testId="dm-message-avatar"
                        />
                        {resolveEmployeeName(employees, message.sender)}
                      </span>
                    ) : null}
                    {/* 内容包裹层：pl-5 与姓名对齐（子元素 w-full 自动收窄，右缘不超出消息列） */}
                    <div
                      className={cn(
                        "flex w-full flex-col",
                        isUser ? "items-end" : "items-start",
                        !isUser && "pl-5",
                      )}
                    >
                      {!isUser && message.process?.length ? (
                        <ProcessBlock nodes={message.process} totalMsOverride={turnDurationMs} />
                      ) : null}
                      <div
                        className={cn(
                          "rounded-[var(--radius-bubble)] px-3.5 py-2.5 text-sm",
                          !isUser && shouldUseDocumentLayout(message.content)
                            ? "w-full max-w-full"
                            : "max-w-[min(720px,88%)]",
                          isUser
                            ? "bg-chat-user-bubble text-chat-ink rounded-tr-[4px]"
                            : "bg-card border-chat-hairline text-chat-ink rounded-tl-[4px] border",
                        )}
                      >
                        <MarkdownRenderer
                          text={message.content}
                          plain={isUser}
                          mentions={mentionNames}
                        />
                      </div>
                      {!isUser ? (
                        <ReplySources
                          nodes={message.process}
                          timestamp={message.created_at}
                          content={message.content}
                        />
                      ) : null}
                    </div>
                  </div>
                );
              })
            )}
            {/* 2026-09-23 用户拍板：正在执行显示在员工头像下方、与头像左缘对齐；
              插队批：thread_progress 帧到达即展示实时过程块（有过程帧优先于静态提示） */}
            {progressNodes.length > 0 || awaitingReply ? (
              <div className="flex flex-col gap-0.5" data-testid="dm-running">
                <EmployeeAvatar
                  name={displayName}
                  metadata={targetEmployee?.metadata}
                  className="size-6 rounded-full text-xs"
                  testId="dm-running-avatar"
                />
                {progressNodes.length > 0 ? (
                  <>
                    <ProcessBlock nodes={progressNodes} />
                    {progressText ? (
                      <p
                        data-testid="dm-running-text"
                        className="text-chat-ink mt-1 whitespace-pre-wrap break-words text-sm"
                      >
                        {progressText}
                      </p>
                    ) : null}
                  </>
                ) : (
                  <span className="text-chat-muted flex items-center gap-1.5 text-xs">
                    <Loader2 className="size-3 animate-spin" />
                    {t("chat.dm.running")}
                  </span>
                )}
              </div>
            ) : null}
          </div>
        </div>
        {!dmAtBottom ? (
          <button
            type="button"
            data-testid="dm-scroll-to-bottom"
            aria-label={t("chat.scrollToLatest")}
            title={t("chat.scrollToLatest")}
            className="border-chat-hairline bg-card text-chat-muted hover:text-chat-ink absolute bottom-5 left-1/2 z-10 flex size-8 -translate-x-1/2 items-center justify-center rounded-full border shadow-md"
            onClick={() => {
              const element = dmScrollRef.current;
              if (element) element.scrollTop = element.scrollHeight;
              dmStickRef.current = true;
              setDmAtBottom(true);
            }}
          >
            <ArrowDown className="size-4" />
          </button>
        ) : null}
      </div>

      {pendingInteraction ? (
        <div className="mx-auto w-full max-w-4xl px-4" data-testid="dm-interaction-stack">
          <InteractionCard
            key={pendingInteraction.interaction_id}
            frame={pendingInteraction}
            onResolved={(id) => useChatStore.getState().resolveInteraction(id)}
            onError={(message) => toast.error(message)}
          />
        </div>
      ) : null}

      {reworkPick ? (
        <div className="mx-auto w-full max-w-4xl px-4">
          <div
            data-testid="dm-rework-chip"
            className="bg-chat-rail/60 mb-0 flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-xs"
          >
            <ListChecks className="text-chat-muted size-3.5 shrink-0" />
            <span className="text-chat-ink min-w-0 flex-1 truncate">
              {t("chat.dm.reworkChip").replace("{title}", reworkPick.title)}
            </span>
            <button
              type="button"
              data-testid="dm-rework-clear"
              className="text-chat-muted hover:text-foreground flex size-4 shrink-0 items-center justify-center"
              title={t("chat.dm.reworkClear")}
              onClick={() => setReworkPick(null)}
            >
              <X className="size-3" />
            </button>
          </div>
        </div>
      ) : null}
      {/* 2026-09-23 用户拍板：对话框与其它会话统一（公共 Composer）；单聊不显示模式控件。
          2026-09-24：busy 从过程槽派生 ⇒ 中断按钮出现；onStop 走 E-6 abort（原 noop 失效）。
          2026-09-30 用户拍板：开 enhanced 拿到控制条（模型/思考模式）+ ⊕ 的上传与@引用文件；
          2026-10-02：单聊不消费「选择员工」（@团队成员仅项目群聊会话提供）——allowTeamPicker=false。 */}
      <Composer
        busy={employeeBusy}
        disabled={retiredLocked}
        showMode
        enhanced
        allowTeamPicker={false}
        attachmentsEnabled
        projectId={projectId}
        // A 方案：员工配了独立模型（llm_config.default_model）时
        // 控制条显示该员工实际使用的模型并锁定切换——全局模型切换对它无效
        employeeModel={employeeOwnModel}
        placeholder={t("chat.dm.placeholder")}
        onSend={send}
        onSteer={send}
        onStop={stopEmployee}
      />
    </div>
  );
}
