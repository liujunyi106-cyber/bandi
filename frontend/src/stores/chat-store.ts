/**
 * 功能：会话域全局状态（zustand，）。
 * 核心机制：
 * - keyed 增量归并：发送时开 pending 槽（`<sessionId>:pending:<ts>`），content_delta 累积
 * 工具事件挂过程节点、done/error 定稿——按槽定位，多会话保留（流式期间切会话不串流）；
 * - 会话/消息缓存在 store；通知中心（events.host 聚合）与挂起交互同店；
 * - 纯函数 reducer 单独导出（行为测试直接覆盖，不依赖 React 渲染）。
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";

import { progressKey } from "@/lib/progress-key";
import type { GroupRoundSnapshot } from "@/services/company";
import { sessionsApi } from "@/services/sessions";
import type { InteractionFrame, SessionEntry, SessionSummary } from "@/services/sessions";

/** CoT 思考节点。 */
export interface ProcessThoughtNode {
  id: string;
  kind: "thought";
  text: string;
  status: "running" | "success";
  /**
   * 思考耗时（毫秒）。：LLM 调用原先无任何耗时字段 ⇒ 纯文本轮
   * `ProcessBlock` 只能累加 tool 节点 ⇒ 恒显示「已完结 · 0.0s」。
   * 借 opencode `ReasoningPart.time.{start,end}`（`packages/tui/src/routes/session/index.tsx:1583`）
   * 让思考本身可度量。
   */
  durationMs?: number;
  /** 起始时间戳（运行中节点用于实时计时；关闭时换算成 durationMs）。 */
  startedAt?: number;
}

/** 工具调用节点。 */
/** 功能：知识库引用（knowledge_search.citations 的展示子集）。 */
export interface CitationRef {
  id?: string;
  label?: string;
  kind?: string;
  title?: string;
  source_path?: string;
  content?: string;
  excerpt?: string;
  summary?: string;
  concept_id?: string;
  document_id?: string;
  bucket_id?: string;
  chunk_id?: string;
}

export interface ProcessToolNode {
  id: string;
  kind: "tool";
  toolName: string;
  input?: Record<string, unknown>;
  status: "running" | "success" | "error" | "skipped";
  detail?: string;
  /** 功能：知识库检索引用（回答下方 chips 数据源）。 */
  citations?: CitationRef[];
  /** 最近一条 tool_progress 帧（视图层格式化展示）。 */
  progress?: { type: string; data: Record<string, unknown> };
  durationMs?: number;
  /** 后端 tool_call_id（-3b：节点级重试定位；旧帧无此字段时缺省 = 不可重试）。 */
  toolCallId?: string;
  /** 子代理嵌套过程（task 工具节点的子树——子代理内部思考/工具节点）。 */
  children?: ProcessNode[];
}

/** Skill 使用组节点。 */
export interface ProcessSkillNode {
  id: string;
  kind: "skill";
  /** 已激活技能名（跨事件去重合并）。 */
  skills: string[];
  /** 注入次数（按事件累计）。 */
  count: number;
  status: "success";
}

/** 过程注记节点（压缩/中断/记忆写入等——统一灰色字段行）。 */
export interface ProcessNoteNode {
  id: string;
  kind: "note";
  /** 注记类型：compaction（正在压缩）/ interrupt（用户中断了）/ memory_written（记忆写入）。 */
  noteType: string;
  /** 记忆写入通道总数（noteType=memory_written 时）。 */
  count?: number;
  /**
   * 本轮实际写入的记忆通道名（`long_term` / `episodic` / `working` / `compaction`）。
   * 渲染层据此分档：只有 `long_term`/`episodic` 才算「记住」，`working`（本轮草稿）
   * 与 `compaction`（压缩纪要）不该混称「记忆」。
   */
  channels?: string[];
  status: "success";
}

/** 过程节点（工具时间线；thought=CoT / tool=工具 / skill=技能组 / note=过程注记）。 */
export type ProcessNode = ProcessThoughtNode | ProcessToolNode | ProcessSkillNode | ProcessNoteNode;

/** 功能：会话活动态（条目右侧状态指示）——running = 执行中，done = 离开时已完成。 */
export type SessionActivity = "running" | "done";
/**
 * 关闭所有运行中的过程节点，并把思考节点的 `startedAt` 换算成 `durationMs`。
 *
 * 思考节点原先无耗时字段，纯文本轮 `ProcessBlock` 恒显示 0.0s。
 * 收敛到单一 helper —— 此前有 6 处各自把 running 改 success，漏一处就会留下
 * 「永远 running」的死节点（无 duration、前端显示转圈不结束）。
 */
export function settleRunningNodes(nodes: ProcessNode[]): ProcessNode[] {
  const now = Date.now();
  return nodes.map((node) => {
    if (node.status !== "running") return node;
    if (node.kind === "thought") return settleThought(node, now);
    return { ...node, status: "success" as const };
  });
}

/**
 * 只结算**运行中的思考**节点（工具节点必须保持 running —— 它确实还在跑）。
 *
 * 用于「思考 → 工具」转换点（`applyToolStart`）。误用全量 `settleRunningNodes`
 * 会把在跑的工具提前置 success，后续 `applyToolProgress` 因找不到 running 节点
 * 而静默丢进度（级回归，由 `applyToolProgress` 既有契约抓出）。
 */
function settleRunningThoughts(nodes: ProcessNode[]): ProcessNode[] {
  const now = Date.now();
  return nodes.map((node) =>
    node.kind === "thought" && node.status === "running" ? settleThought(node, now) : node,
  );
}

/** 关闭单个思考节点并结算耗时（幂等：已关闭则原样返回）。 */
function settleThought(node: ProcessThoughtNode, now: number = Date.now()): ProcessThoughtNode {
  if (node.status !== "running") return node;
  const startedAt = node.startedAt;
  const durationMs =
    typeof startedAt === "number" && Number.isFinite(startedAt) ? Math.max(0, now - startedAt) : 0;
  const { startedAt: _drop, ...rest } = node;
  return { ...rest, status: "success", durationMs };
}

/** 任务台账条目。 */
export interface TaskLedgerItem {
  id: string;
  title: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
}

/** 员工执行过程事件。 */
export interface EmployeeProgressEvent {
  type: string;
  data?: Record<string, unknown>;
}

/** 功能：过程事件序列 → 过程节点（持久化回放——刷新后过程块可回看）。 */
export function nodesFromEvents(events: unknown[]): ProcessNode[] {
  let nodes: ProcessNode[] = [];
  for (const raw of events) {
    if (!raw || typeof raw !== "object") continue;
    const event = raw as { type?: unknown; data?: unknown };
    if (typeof event.type !== "string") continue;
    nodes = applyProgressNode(nodes, {
      type: event.type,
      data: (event.data as Record<string, unknown> | undefined) ?? {},
    });
  }
  return nodes;
}

/** 功能：员工过程事件 → 过程节点归并。 */
export function applyProgressNode(
  nodes: ProcessNode[],
  event: EmployeeProgressEvent,
): ProcessNode[] {
  const next = [...nodes];
  const data = event.data ?? {};
  switch (event.type) {
    case "thinking_delta": {
      const text = String(data.text ?? "");
      if (!text) return next;
      const last = next[next.length - 1];
      if (last && last.kind === "thought" && last.status === "running") {
        next[next.length - 1] = { ...last, text: last.text + text };
      } else {
        next.push({
          id: `ep:thought:${next.length + 1}`,
          kind: "thought",
          text,
          status: "running",
          startedAt: Date.now(),
        });
      }
      return next;
    }
    case "started": {
      if (next.some((node) => node.id === "ep:started")) return next;
      next.push({
        id: "ep:started",
        kind: "thought",
        text: "",
        status: "running",
        startedAt: Date.now(),
      });
      return next;
    }
    case "tool_call_start": {
      for (let index = 0; index < next.length; index += 1) {
        const node = next[index];
        if (node.kind === "thought" && node.status === "running") {
          next[index] = settleThought(node);
        }
      }
      next.push({
        id: `ep:tool:${next.length + 1}`,
        kind: "tool",
        toolName: String(data.tool_name ?? ""),
        input: (data.tool_input as Record<string, unknown> | undefined) ?? {},
        status: "running",
        toolCallId: data.tool_call_id ? String(data.tool_call_id) : undefined,
      });
      return next;
    }
    case "tool_progress": {
      const toolName = String(data.tool_name ?? "");
      for (let index = next.length - 1; index >= 0; index -= 1) {
        const node = next[index];
        if (node.kind === "tool" && node.toolName === toolName && node.status === "running") {
          next[index] = {
            ...node,
            progress: {
              type: String(data.progress_type ?? ""),
              data: (data.data as Record<string, unknown> | undefined) ?? {},
            },
          };
          break;
        }
      }
      return next;
    }
    case "tool_result": {
      const toolName = String(data.tool_name ?? "");
      for (let index = next.length - 1; index >= 0; index -= 1) {
        const node = next[index];
        if (node.kind === "tool" && node.toolName === toolName && node.status === "running") {
          next[index] = {
            ...node,
            status: Boolean(data.success) ? ("success" as const) : ("error" as const),
            detail: String(data.output_preview ?? ""),
            durationMs: Number(data.duration_ms ?? 0),
          };
          break;
        }
      }
      return next;
    }
    case "skill_injected": {
      const skills = Array.isArray(data.skills) ? (data.skills as unknown[]).map(String) : [];
      if (!skills.length) return next;
      const index = next.findIndex((node) => node.kind === "skill");
      const current = index >= 0 ? next[index] : null;
      if (current && current.kind === "skill") {
        next[index] = {
          ...current,
          skills: Array.from(new Set([...current.skills, ...skills])),
          count: current.count + 1,
        };
      } else {
        next.unshift({
          id: "ep:skill:1",
          kind: "skill",
          skills,
          count: 1,
          status: "success",
        });
      }
      return next;
    }
    case "memory_written": {
      const channels = Array.isArray(data.channels) ? (data.channels as unknown[]).map(String) : [];
      if (!channels.length) return next;
      const last = next[next.length - 1];
      if (last && last.kind === "note" && last.noteType === "memory_written") return next;
      next.push({
        id: `ep:note:${next.length + 1}`,
        kind: "note",
        noteType: "memory_written",
        count: channels.length,
        status: "success",
      });
      return next;
    }
    case "note": {
      const noteType = String(data.note_type ?? "");
      if (!noteType) return next;
      const last = next[next.length - 1];
      if (last && last.kind === "note" && last.noteType === noteType) return next;
      next.push({
        id: `ep:note:${next.length + 1}`,
        kind: "note",
        noteType,
        status: "success",
      });
      return next;
    }
    case "done":
    case "error": {
      // 终态收尾（真机实证：done/error 落入 default 后省略收尾，快照回放的
      // running 节点永久「正在执行」——刷新后残留）；全部未结节点结为已结束。
      return next.map((node) => {
        if (node.kind === "thought" && node.status === "running") return settleThought(node);
        if (node.kind === "tool" && node.status === "running") {
          return { ...node, status: "success" as const };
        }
        return node;
      });
    }
    default:
      return next;
  }
}

/** 消息流条目（气泡模型；process = 过程块节点）。 */
export interface ChatMessage {
  id: string;
  sessionId: string;
  role: string;
  content: string;
  done: boolean;
  error?: string;
  turnIndex?: number;
  createdAt?: string;
  /** 首帧到达/占位创建时刻——卡片完成时间 = completedAt − startedAt（总执行时长）。 */
  startedAt?: string;
  completedAt?: string;
  process?: ProcessNode[];
  /** 本地发出（尚未从后端列表读回）。 */
  local?: boolean;
  /**
   * 后端会话条目 id（fork/revert 等按 entry 定位的操作只在此存在时可用）——
   * 本地待定稿消息的 `id` 是前端临时键（`<session>:pending:<ts>`），拿去调 entry 级 API 必失败。
   */
  entryId?: string;
  /** 消息级反馈（本地暂存；记忆条目直连留 ）。 */
  feedback?: "up";
  /** 员工回复标识（thread_message 帧注入——头像/名字呈现）。 */
  senderName?: string;
  senderRole?: string;
  /** 该回复对应的工作项 id（线程帧 message_id = work item id）——成员任务卡据此
   * 关联子卡（parent_id 匹配）。 */
  workItemId?: string;
  /** 对话附件。 */
  attachments?: ChatAttachment[];
    /** 回复底部「下一步」交接行：下一个待执行卡与执行者。 */
    nextAssignee?: { roleId: string; card: string };
}

/** 对话附件引用（发送 payload / 历史 metadata 共用形态）。 */
export interface ChatAttachment {
  upload_id: string;
  name: string;
  size: number;
}

/** 执行中待发送消息（文本必须点“立即发送”；带附件回合结束自动发送）。 */
export interface PendingSend {
  id: string;
  text: string;
  attachments: ChatAttachment[];
  createdAt: string;
}

/** 草稿附件。 */
export interface PendingAttachment {
  upload_id: string;
  name: string;
  size: number;
  status: "uploading" | "done" | "error";
}

/** 草稿「文件引用」（@ 选项目文件——chip 展示，发送时转 `@路径` 文本注入；）。 */
export interface DraftReference {
  path: string;
  name: string;
}

/** 流式槽位（按会话并行——每会话一条，互不阻塞）。 */
export interface StreamSlot {
  sessionId: string;
  pendingId: string;
}

/** 通知中心条目（events.host 聚合：会话完成/错误/交互/系统）。 */
export interface NotificationItem {
  id: string;
  kind: "done" | "error" | "interaction" | "info";
  title: string;
  body?: string;
  sessionId?: string;
  ts: number;
  read: boolean;
}

export interface StagedRevert {
  sessionId: string;
  entryId: string;
}

interface ChatState {
  sessions: SessionSummary[];
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  /** 当前选中项目（New Chat 归属；空串 = 无项目）。 */
  activeProjectId: string;
  /** 新会话来源标记（"secretary" = 秘书入口发起；发送一次后清空）。 */
  pendingSessionOrigin: string;
  /** 置顶会话（排序靠前；本地状态）。 */
  pinnedSessionIds: string[];
  /** 会话活动态（会话条目右侧圆点：running = 进行中转圈，done = 已完成灰点）。 */
  sessionActivity: Record<string, SessionActivity>;
  /** 每会话最近一轮终态（后端 `terminal_reason` 原值）——中断横幅按原因分档。 */
  sessionTerminals: Record<string, string>;
  /** 已从项目列表移除的项目，其下旧会话 id 快照（project_id → session_id 列表；本地隐藏位）。
   * 语义：移除项目 = 该项目解绑且旧会话不再显示；后端记录与磁盘文件不动，
   * 重新加入该项目是全新起点（新建会话 id 不在本表内，照常显示）。 */
  dismissedProjectSessions: Record<string, string[]>;
  messagesBySession: Record<string, ChatMessage[]>;
  /** 按会话流槽——A 会话回复中不阻塞 B 会话发送。 */
  streams: Record<string, StreamSlot>;
  interactions: Record<string, InteractionFrame>;
  notifications: NotificationItem[];
  stagedRevert: StagedRevert | null;
  /** 会话任务台账。 */
  tasksBySession: Record<string, TaskLedgerItem[]>;
  /** 群聊回合状态（group_round 帧 / GET 回合快照）——key = session_id */
  roundsBySession: Record<string, GroupRoundSnapshot>;
  /** 员工执行过程（thread_progress 帧——key = `${project_id}::${role_id}`；过程块归属） */
  employeeProgress: Record<string, ProcessNode[]>;
  /** 员工执行中的正文流（content_delta 累积，回复落位后清空——首 token 即上屏） */
  employeeProgressText: Record<string, string>;
  /** 员工过程槽「已完成」标记（done/error 后置位；新执行首帧到达即清槽重建——过程块保留可回顾）。 */
  employeeProgressDone: Record<string, boolean>;
  /** 执行中待发送队列（按会话隔离；文本需手动立即发送，附件回合结束自动发送）。 */
  pendingSendsBySession: Record<string, PendingSend[]>;
  /** 草稿附件。 */
  draftAttachments: PendingAttachment[];
  /** 草稿文件引用（Composer 引用 chips；发送时转 @路径 文本——）。 */
  draftReferences: DraftReference[];

  setSessions: (sessions: SessionSummary[]) => void;
  upsertSession: (session: SessionSummary) => void;
  removeSessionLocal: (sessionId: string) => void;
  setActiveSession: (sessionId: string | null) => void;
  setActiveProject: (projectId: string) => void;
  setPendingSessionOrigin: (origin: string) => void;
  togglePin: (sessionId: string) => void;
  /** 功能：标记会话活动态（进行中 / 已完成未读）——驱动会话条目右侧圆点。 */
  markSessionActivity: (sessionId: string, state: "running" | "done" | "idle") => void;
  /** 功能：清某会话活动标记（用户进入该会话后调用）。 */
  clearSessionActivity: (sessionId: string) => void;
  /** 记录最近一轮终态（`terminal_reason` 原值），供中断横幅分档。 */
  markSessionTerminal: (sessionId: string, terminalReason: string) => void;
  dismissProjectSessions: (projectId: string, sessionIds: string[]) => void;
  loadMessages: (sessionId: string, messages: ChatMessage[]) => void;
  /** 本地系统提示条目（转向注入回执等）。 */
  appendSystemNote: (sessionId: string, content: string) => void;
  /** 功能：清掉本地系统提示（员工回复落位后「已转交员工」等过渡注记应消失）。 */
  clearTransientNotes: (sessionId: string) => void;
  /** 员工工作回复注入（thread_message 帧——团队会话/群聊的消息流呈现；
   * process = 该员工本次执行过程。 */
  appendEmployeeReply: (
    sessionId: string,
    reply: {
      name: string;
      roleId: string;
      content: string;
      process?: ProcessNode[];
  workItemId?: string;
  /** 交接：本轮之后下一个待执行卡的执行者（回复底部「下一步」行）。 */
  nextAssignee?: { roleId: string; card: string };
    },
  ) => void;
  appendLocalUser: (sessionId: string, content: string, attachments?: ChatAttachment[]) => string;
  /** 功能：执行中待发送队列（按会话隔离）。 */
  enqueuePendingSend: (
    sessionId: string,
    text: string,
    attachments?: ChatAttachment[],
  ) => string;
  removePendingSend: (sessionId: string, id: string) => void;
  clearPendingSends: (sessionId: string) => void;
  /** 功能：草稿附件增删改清。 */
  addDraftAttachment: (item: PendingAttachment) => void;
  updateDraftAttachment: (uploadId: string, patch: Partial<PendingAttachment>) => void;
  removeDraftAttachment: (uploadId: string) => void;
  clearDraftAttachments: () => void;
  clearSentAttachments: () => void;
  /** 功能：草稿文件引用增删清（@ 引用项目文件 chips；发送时转 @路径 文本——）。 */
  addDraftReference: (item: DraftReference) => void;
  removeDraftReference: (path: string) => void;
  clearDraftReferences: () => void;
  appendDelta: (
    sessionId: string,
    text: string,
    options?: { createPlaceholder?: boolean },
  ) => void;
  applyThinking: (
    sessionId: string,
    text: string,
    options?: { createPlaceholder?: boolean },
  ) => void;
  applyNote: (sessionId: string, noteType: string) => void;
  /** 功能：子代理嵌套过程帧（挂到对应 task 工具节点的 children 子树）。 */
  applySubagentProgress: (
    sessionId: string,
    subagent: { id: string; task?: string; parent_tool_call_id?: string },
    kind: string,
    payload: Record<string, unknown>,
  ) => void;
  applyMemoryWritten: (sessionId: string, channels: string[]) => void;
  setTaskLedger: (sessionId: string, items: TaskLedgerItem[]) => void;
  /** 写入群聊回合状态（帧与初始拉取同源） */
  applyRound: (round: GroupRoundSnapshot) => void;
  /** 清除会话回合状态（切换/删除会话） */
  clearRound: (sessionId: string) => void;
  /**
   * 功能：员工过程槽写入（`` 起槽键 = 项目::线程::员工）。
   * 线程维度来自过程帧/回写同源的 `thread_id`（见 `lib/progress-key.ts`）。
   */
  applyEmployeeProgress: (
    projectId: string,
    threadId: string,
    roleId: string,
    event: EmployeeProgressEvent,
  ) => void;
  finishEmployeeProgress: (projectId: string, threadId: string, roleId: string) => void;
  /** 功能：取出并清空员工过程槽。 */
  consumeEmployeeProgress: (projectId: string, threadId: string, roleId: string) => ProcessNode[];
  clearEmployeeProgress: (projectId: string, threadId: string, roleId: string) => void;
  /**
   * 功能：兜底过期清槽——**仅当该槽处于「已完成」标记**时清。
   * 完成态若长时间无人消费（用户不在消费视图 / 该工作项未产出线程消息）
   * 过程块会永久悬浮在视图底部；由 host 层延时调度本动作兜底
   * 且因只在 done 标记下生效，不会误清「已开始新执行」的槽。
   */
  expireEmployeeProgressIfDone: (projectId: string, threadId: string, roleId: string) => void;
  /** 功能：按槽键兜底清槽（完成标记下）——断线结算拿到的是键，避免二次拆分/拼回。 */
  expireEmployeeProgressByKey: (key: string) => void;
  /**
   * 功能：断线结算——链路断开时，把「仍在执行中」的过程槽收敛为完成态并
   * 附「连接中断，过程可能不完整」注记，**返回被结算的槽键**（host 据此挂兜底过期定时器）。
   * 存在意义：过程帧中间态不进补发缓冲 ⇒ 断线期间错过的过程帧补不回来，该槽会一直显示
   * 「执行中」。若该次执行其实还在跑，后续帧会让槽清空重建（首帧清 done 标记）⇒ 自愈；
   * 若确实已结束，回复到达时仍能消费到（部分）过程块，兜底定时器则负责最终回收。
   */
  settleStaleEmployeeProgress: () => string[];
  applySkillInjected: (sessionId: string, skills: string[]) => void;
  applyToolStart: (
    sessionId: string,
    toolName: string,
    toolInput?: Record<string, unknown>,
    toolCallId?: string,
  ) => void;
  applyToolProgress: (
    sessionId: string,
    event: { toolName: string; progressType: string; data: Record<string, unknown> },
  ) => void;
  applyToolResult: (
    sessionId: string,
    event: {
      toolName: string;
      success: boolean;
      preview: string;
      durationMs: number;
      toolCallId?: string;
      citations?: CitationRef[];
    },
  ) => void;
  /** 功能：本地跳过失败工具节点（-3b：灰显「已跳过」；3c 随过程持久化）。 */
  skipToolNode: (sessionId: string, nodeId: string) => void;
  /** 功能：节点级重试结果回填（-3b：按 toolCallId 更新前端内存节点）。 */
  applyToolRetry: (
    sessionId: string,
    toolCallId: string,
    result: { success: boolean; outputPreview: string; durationMs: number; error?: string },
  ) => void;
  finalizeStream: (sessionId: string, patch: { turnIndex?: number; error?: string }) => void;
  failStream: (sessionId: string, error: string) => void;
  /** 功能：本地兜底中断——后端确认无活动流（404）/超时未见终结帧时把挂起槽定稿并
   * 释放（空占位直接移除），防「正在回复中」永久残留。按会话定位槽。 */
  abortLocalStream: (sessionId: string) => void;
  addInteraction: (frame: InteractionFrame) => void;
  resolveInteraction: (interactionId: string) => void;
  pushNotification: (item: Omit<NotificationItem, "id" | "ts" | "read"> & { id?: string }) => void;
  markNotificationsRead: () => void;
  clearNotifications: () => void;
  setStagedRevert: (staged: StagedRevert | null) => void;
  setFeedback: (sessionId: string, messageId: string, value: "up" | null) => Promise<void>;
}

const NOTIFICATION_CAP = 100;

// ── 纯函数 reducer（行为测试直接覆盖）──────────────────────────────────────

/** 功能：解析条目 metadata 中的附件列表。 */
export function parseEntryAttachments(metadata: unknown): ChatAttachment[] | undefined {
  let raw: unknown = metadata;
  if (typeof raw === "string") {
    if (!raw) return undefined;
    try {
      raw = JSON.parse(raw);
    } catch {
      return undefined;
    }
  }
  if (!raw || typeof raw !== "object") return undefined;
  const list = (raw as { attachments?: unknown }).attachments;
  if (!Array.isArray(list)) return undefined;
  const items = list
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item) => ({
      upload_id: String(item.upload_id ?? ""),
      name: String(item.name ?? ""),
      size: Number(item.size ?? 0),
    }))
    .filter((item) => item.upload_id && item.name);
  return items.length ? items : undefined;
}

/** 功能：会话条目 → 气泡模型（只保留有内容的 user/assistant/system 条目）。 */
/** 功能：process 条目 metadata 还原为过程节点。 */
export function parseEntryProcessNodes(metadata: unknown): ProcessNode[] | undefined {
  let raw: unknown = metadata;
  if (typeof raw === "string") {
    if (!raw) return undefined;
    try {
      raw = JSON.parse(raw);
    } catch {
      return undefined;
    }
  }
  if (!raw || typeof raw !== "object") return undefined;
  const nodes = (raw as Record<string, unknown>).process;
  if (!Array.isArray(nodes) || nodes.length === 0) return undefined;
  return nodes.filter(
    (node): node is ProcessNode =>
      !!node &&
      typeof node === "object" &&
      typeof (node as Record<string, unknown>).kind === "string",
  );
}

export function entriesToMessages(sessionId: string, entries: SessionEntry[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const entry of entries) {
    const role = String(entry.role);
    if (entry.entry_type === "process") {
      // -3c：过程条目挂到前一条 assistant 消息（轮末落库序与对话序一致）
      const nodes = parseEntryProcessNodes(entry.metadata);
      const target = [...messages].reverse().find((item) => item.role === "assistant");
      if (target && nodes) target.process = target.process ?? nodes;
      continue;
    }
    if (!["user", "assistant", "system"].includes(role)) continue;
    if (!String(entry.content ?? "").trim()) continue;
    const attachments = parseEntryAttachments(entry.metadata);
    // 员工产出回写条目（后端 append_employee_reply）→ 带发送者身份渲染
    // （头像 tone 走 senderRole、名字优先 senderName 快照）——与 live thread_message 注入同构
    const employeeReply = parseEntryEmployeeReply(entry.metadata);
    messages.push({
      id: entry.id,
      sessionId,
      role,
      content: String(entry.content ?? ""),
      done: true,
      turnIndex: entry.turn_index,
      createdAt: entry.created_at,
      entryId: entry.id,
      ...(employeeReply ?? {}),
      ...(attachments ? { attachments } : {}),
    });
  }
  return messages;
}

/** 功能：员工回写条目的身份标记（后端 metadata：employee_reply/sender_role/sender_name/
 * process_events/work_item_id——过程快照随回复持久化，刷新后过程块回放；
 * work_item_id 供成员任务卡/交付物卡片按工作项匹配）。 */
function parseEntryEmployeeReply(
  metadata: unknown,
): Pick<ChatMessage, "senderRole" | "senderName" | "process" | "workItemId" | "nextAssignee"> | null {
  let raw: unknown = metadata;
  if (typeof raw === "string") {
    if (!raw) return null;
    try {
      raw = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!raw || typeof raw !== "object") return null;
  const record = raw as {
    employee_reply?: unknown;
    sender_role?: unknown;
    sender_name?: unknown;
    process_events?: unknown;
    work_item_id?: unknown;
    next_assignee?: unknown;
  };
  // 功能：仅显式 employee_reply 标记才视为员工回复（普通助手条目不得误标身份）
  if (record.employee_reply !== true) return null;
  const senderRole = String(record.sender_role ?? "").trim();
  const senderName = String(record.sender_name ?? "").trim();
  const workItemId = String(record.work_item_id ?? "").trim();
  const nextRaw =
    record.next_assignee && typeof record.next_assignee === "object"
      ? (record.next_assignee as { role_id?: unknown; card?: unknown })
      : null;
  const nextAssignee = nextRaw
    ? { roleId: String(nextRaw.role_id ?? "").trim(), card: String(nextRaw.card ?? "").trim() }
    : null;
  // 历史固化收尾：存档在回写时机截断（执行中途），尾部无 done 帧 ⇒ 残留 running
  // 是死状态（刷新后过程块永久「执行中」）——重建时统一结算。
  const process = settleRunningNodes(
    nodesFromEvents(Array.isArray(record.process_events) ? record.process_events : []),
  );
  return {
    ...(senderRole ? { senderRole } : {}),
    ...(senderName ? { senderName } : {}),
    ...(workItemId ? { workItemId } : {}),
    ...(process.length ? { process } : {}),
    ...(nextAssignee && nextAssignee.roleId ? { nextAssignee } : {}),
  };
}

/** 功能：向指定 pending 槽累积文本（keyed 增量——未命中槽返回原列表）；首帧记 startedAt。 */
export function applyDelta(
  messages: ChatMessage[],
  pendingId: string,
  text: string,
): ChatMessage[] {
  return messages.map((message) =>
    message.id === pendingId
      ? {
          ...message,
          content: message.content + text,
          startedAt: message.startedAt ?? new Date().toISOString(),
        }
      : message,
  );
}

/** 功能：追加 CoT 思考增量——挂到最后一个进行中的 thought 节点，无则开新节点。 */
export function applyThinking(
  messages: ChatMessage[],
  pendingId: string,
  text: string,
): ChatMessage[] {
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    const last = nodes[nodes.length - 1];
    if (last && last.kind === "thought" && last.status === "running") {
      nodes[nodes.length - 1] = { ...last, text: last.text + text };
    } else {
      nodes.push({
        id: `${pendingId}:thought:${nodes.length + 1}`,
        kind: "thought",
        text,
        status: "running",
        startedAt: Date.now(),
      });
    }
    return {
      ...message,
      startedAt: message.startedAt ?? new Date().toISOString(),
      process: nodes,
    };
  });
}

/** 功能：Skill 注入归并——合并技能名并累加次数；节点插过程块首位。 */
export function applySkillInjected(
  messages: ChatMessage[],
  pendingId: string,
  skills: string[],
): ChatMessage[] {
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    const index = nodes.findIndex((node) => node.kind === "skill");
    const current = index >= 0 ? nodes[index] : null;
    if (current && current.kind === "skill") {
      nodes[index] = {
        ...current,
        skills: Array.from(new Set([...current.skills, ...skills])),
        count: current.count + 1,
      };
    } else {
      nodes.unshift({
        id: `${pendingId}:skill:1`,
        kind: "skill",
        skills: [...skills],
        count: 1,
        status: "success",
      });
    }
    return { ...message, process: nodes };
  });
}

/** 功能：过程注记追加（相邻同类去重；压缩/中断等——统一灰色）。 */
export function applyNote(
  messages: ChatMessage[],
  pendingId: string,
  noteType: string,
): ChatMessage[] {
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    const last = nodes[nodes.length - 1];
    if (last && last.kind === "note" && last.noteType === noteType) return message;
    nodes.push({
      id: `${pendingId}:note:${nodes.length + 1}`,
      kind: "note",
      noteType,
      status: "success",
    });
    return { ...message, process: nodes };
  });
}

/** 功能：任务台账归一化（实时帧与 session metadata 共用；状态别名对齐后端 normalize_todo_status）。 */
export function normalizeTaskLedger(raw: unknown): TaskLedgerItem[] {
  if (!Array.isArray(raw)) return [];
  const doneStatuses = new Set(["done", "completed", "complete", "finished"]);
  const activeStatuses = new Set(["in_progress", "in-progress", "active", "current"]);
  const items: TaskLedgerItem[] = [];
  raw.forEach((entry, index) => {
    if (!entry || typeof entry !== "object") return;
    const record = entry as Record<string, unknown>;
    const rawStatus = String(record.status ?? "pending")
      .trim()
      .toLowerCase();
    const status: TaskLedgerItem["status"] = doneStatuses.has(rawStatus)
      ? "completed"
      : activeStatuses.has(rawStatus)
        ? "in_progress"
        : "pending";
    const title = String(record.title ?? record.content ?? `todo-${index + 1}`);
    items.push({
      id: String(record.id ?? index + 1),
      title,
      content: String(record.content ?? title),
      status,
    });
  });
  return items;
}

/** 功能：从会话详情 session.mode 解析对话模式（缺省 execute；非法值回落 execute）。 */
export function sessionChatMode(session: Record<string, unknown> | undefined): "plan" | "execute" {
  const raw = session?.mode;
  return typeof raw === "string" && raw.trim().toLowerCase() === "plan" ? "plan" : "execute";
}

/** 功能：从会话详情 session.metadata 解析任务台账（刷新后任务条恢复；损坏静默空）。 */
export function sessionTaskLedger(session: Record<string, unknown> | undefined): TaskLedgerItem[] {
  if (!session) return [];
  const raw = session.metadata;
  let meta: Record<string, unknown> | null = null;
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw) as unknown;
      meta = parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
    } catch {
      meta = null;
    }
  } else if (raw && typeof raw === "object") {
    meta = raw as Record<string, unknown>;
  }
  return normalizeTaskLedger(meta?.task_ledger);
}

/** 功能：记忆写入行追加（按通道分档文案；空通道不展示、相邻同类型去重）。 */
export function applyMemoryWritten(
  messages: ChatMessage[],
  pendingId: string,
  channels: string[],
): ChatMessage[] {
  if (!channels.length) return messages;
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    const last = nodes[nodes.length - 1];
    if (last && last.kind === "note" && last.noteType === "memory_written") return message;
    nodes.push({
      id: `${pendingId}:note:${nodes.length + 1}`,
      kind: "note",
      noteType: "memory_written",
      // 通道名随行留存——渲染层据此分档（长期记忆 vs 本轮草稿/纪要不该混称「记忆」）
      channels: [...channels],
      count: channels.length,
      status: "success",
    });
    return { ...message, process: nodes };
  });
}

/** 功能：在 pending 槽追加一个工具过程节点（running；工具开始即定稿前序思考；-3b 携带 toolCallId）。 */
export function applyToolStart(
  messages: ChatMessage[],
  pendingId: string,
  toolName: string,
  toolInput: Record<string, unknown> = {},
  toolCallId = "",
): ChatMessage[] {
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    // 晚到的 tool_call_start 若落在**已终态**的消息上
    // （done 帧先到、工具帧后到；或流看门狗已本地收尾），节点会永远停在 running
    // ⇒ ProcessBlock 的 running 恒真 ⇒ 耗时永不显示 + 过程块永不自动折叠
    // （证据：回合完成后 total="" 且出现 `list_dir ×1 glob ×1`）。
    // 已终态的消息只能追加**已结算**节点——没有任何后续帧会来收尾它。
    const settled = message.done === true;
    const nodes = settleRunningThoughts(message.process ?? []);
    return {
      ...message,
      startedAt: message.startedAt ?? new Date().toISOString(),
      process: [
        ...nodes,
        {
          id: `${pendingId}:tool:${nodes.length + 1}`,
          kind: "tool" as const,
          toolName,
          input: toolInput,
          status: settled ? ("success" as const) : ("running" as const),
          toolCallId: toolCallId || undefined,
        },
      ],
    };
  });
}

/**
 * 功能：子代理嵌套过程——把子代理帧挂到对应 task 工具节点的 children 子树。
 * 找不到父节点（帧序异常/无匹配 tool_call_id）时忽略（fail-safe，不挂错树）。
 */
export function applySubagentProgress(
  messages: ChatMessage[],
  pendingId: string,
  subagent: { id: string; task?: string; parent_tool_call_id?: string },
  kind: string,
  payload: Record<string, unknown>,
): ChatMessage[] {
  if (kind === "content") return messages;
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    const parentId = subagent.parent_tool_call_id ?? "";
    const parentIndex = nodes.findIndex(
      (node) => node.kind === "tool" && Boolean(parentId) && node.toolCallId === parentId,
    );
    if (parentIndex < 0) return message;
    const parent = nodes[parentIndex] as ProcessToolNode;
    const children = applyChildProgress([...(parent.children ?? [])], parent.id, kind, payload);
    nodes[parentIndex] = { ...parent, children };
    return { ...message, process: nodes };
  });
}

/** 功能：child 子树单步累积。 */
function applyChildProgress(
  children: ProcessNode[],
  parentNodeId: string,
  kind: string,
  payload: Record<string, unknown>,
): ProcessNode[] {
  const nextId = () => `${parentNodeId}:sa:${children.length + 1}`;
  switch (kind) {
    case "thinking": {
      const last = children[children.length - 1];
      if (last && last.kind === "thought" && last.status === "running") {
        const updated = [...children];
        updated[updated.length - 1] = { ...last, text: last.text + String(payload.text ?? "") };
        return updated;
      }
      return [
        ...children,
        {
          id: nextId(),
          kind: "thought",
          text: String(payload.text ?? ""),
          status: "running",
          startedAt: Date.now(),
        },
      ];
    }
    case "tool_start":
      return [
        ...children,
        {
          id: nextId(),
          kind: "tool",
          toolName: String(payload.tool_name ?? "?"),
          input: (payload.tool_input as Record<string, unknown>) ?? {},
          status: "running",
          toolCallId: String(payload.tool_call_id ?? "") || undefined,
        },
      ];
    case "tool_result": {
      const toolCallId = String(payload.tool_call_id ?? "");
      const toolName = String(payload.tool_name ?? "");
      for (let i = children.length - 1; i >= 0; i -= 1) {
        const node = children[i];
        if (node.kind !== "tool" || node.status !== "running") continue;
        const matches = toolCallId ? node.toolCallId === toolCallId : node.toolName === toolName;
        if (!matches) continue;
        const updated = [...children];
        updated[i] = {
          ...node,
          status: payload.success === false ? ("error" as const) : ("success" as const),
          detail: String(payload.output_preview ?? ""),
          durationMs: Number(payload.duration_ms ?? 0),
        };
        return updated;
      }
      return children;
    }
    case "tool_progress": {
      for (let i = children.length - 1; i >= 0; i -= 1) {
        const node = children[i];
        if (node.kind !== "tool" || node.status !== "running") continue;
        const updated = [...children];
        updated[i] = {
          ...node,
          progress: {
            type: String(payload.progress_type ?? ""),
            data: (payload.data as Record<string, unknown>) ?? {},
          },
        };
        return updated;
      }
      return children;
    }
    case "skill": {
      const skills = Array.isArray(payload.skills)
        ? (payload.skills as unknown[]).map((item) => String(item))
        : [];
      if (skills.length === 0) return children;
      return [...children, { id: nextId(), kind: "skill", skills, count: 1, status: "success" }];
    }
    case "note":
      return [
        ...children,
        {
          id: nextId(),
          kind: "note",
          noteType: String(payload.note_type ?? ""),
          status: "success",
        },
      ];
    default:
      return children;
  }
}

/** 功能：工具进度回填——匹配最后一个同名 running 工具节点。 */ export function applyToolProgress(
  messages: ChatMessage[],
  pendingId: string,
  event: { toolName: string; progressType: string; data: Record<string, unknown> },
): ChatMessage[] {
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    for (let index = nodes.length - 1; index >= 0; index -= 1) {
      const node = nodes[index];
      if (node.kind === "tool" && node.toolName === event.toolName && node.status === "running") {
        nodes[index] = { ...node, progress: { type: event.progressType, data: event.data } };
        break;
      }
    }
    return { ...message, process: nodes };
  });
}

/** 功能：工具结果回填——匹配最后一个同名 running 工具节点（LIFO，防并发同名串位；-3b 回填 toolCallId）。 */
export function applyToolResult(
  messages: ChatMessage[],
  pendingId: string,
  event: {
    toolName: string;
    success: boolean;
    preview: string;
    durationMs: number;
    toolCallId?: string;
    citations?: CitationRef[];
  },
): ChatMessage[] {
  return messages.map((message) => {
    if (message.id !== pendingId) return message;
    const nodes = [...(message.process ?? [])];
    for (let index = nodes.length - 1; index >= 0; index -= 1) {
      const node = nodes[index];
      if (node.kind === "tool" && node.toolName === event.toolName && node.status === "running") {
        nodes[index] = {
          ...node,
          status: event.success ? "success" : "error",
          detail: event.preview,
          durationMs: event.durationMs,
          toolCallId: event.toolCallId || node.toolCallId,
          citations: event.citations ?? [],
        };
        break;
      }
    }
    return { ...message, process: nodes };
  });
}

/** 功能：本地跳过失败工具节点（-3b：灰显「已跳过」；本地状态，3c 随过程持久化）。 */
export function markToolSkipped(messages: ChatMessage[], nodeId: string): ChatMessage[] {
  return messages.map((message) => {
    if (!message.process?.some((node) => node.id === nodeId)) return message;
    return {
      ...message,
      process: message.process.map((node) =>
        node.id === nodeId && node.kind === "tool" && node.status === "error"
          ? { ...node, status: "skipped" as const }
          : node,
      ),
    };
  });
}

/** 功能：节点级重试结果回填（-3b）——按 toolCallId 定位失败节点并写入新结果
 * （只更新前端内存节点，不接回 Agent 循环、不写入 LLM 上下文——设计决策登记）。 */
export function applyToolRetryResult(
  messages: ChatMessage[],
  toolCallId: string,
  result: { success: boolean; outputPreview: string; durationMs: number; error?: string },
): ChatMessage[] {
  return messages.map((message) => {
    if (!message.process?.some((node) => node.kind === "tool" && node.toolCallId === toolCallId)) {
      return message;
    }
    return {
      ...message,
      process: message.process.map((node) => {
        if (node.kind !== "tool" || node.toolCallId !== toolCallId) return node;
        return {
          ...node,
          status: result.success ? ("success" as const) : ("error" as const),
          detail: result.success ? result.outputPreview : result.error || result.outputPreview,
          durationMs: result.durationMs,
        };
      }),
    };
  });
}

/** 功能：定稿 pending 槽（done/error；turnIndex 回填；仍 running 的过程节点收敛为 success）。
 * completedAt = 定稿墙钟时刻，与消息起点（首帧或占位创建）相减得总执行时长。 */
export function finalizeMessage(
  messages: ChatMessage[],
  pendingId: string,
  patch: { turnIndex?: number; error?: string },
): ChatMessage[] {
  return messages.map((message) =>
    message.id === pendingId
      ? {
          ...message,
          done: true,
          turnIndex: patch.turnIndex ?? message.turnIndex,
          error: patch.error,
          completedAt: new Date().toISOString(),
          startedAt: message.startedAt ?? message.createdAt ?? message.completedAt,
          process: message.process ? settleRunningNodes(message.process) : undefined,
        }
      : message,
  );
}

// ── store ──────────────────────────────────────────────────────────────────

function updatePending(
  state: ChatState,
  sessionId: string,
  reducer: (messages: ChatMessage[], pendingId: string) => ChatMessage[],
  options?: { createPlaceholder?: boolean },
): Partial<ChatState> {
  const slot = state.streams[sessionId];
  if (slot) {
    const list = state.messagesBySession[sessionId] ?? [];
    return {
      messagesBySession: {
        ...state.messagesBySession,
        [sessionId]: reducer(list, slot.pendingId),
      },
    };
  }
  // 跨槽/无槽帧（多轮回合后续 turn、断线补发、广播）：按帧自带的会话 id 归并，
  // 目标是会话内最后一条未定稿 assistant 消息；内容型帧可新建占位（不丢 token）
  const list = state.messagesBySession[sessionId] ?? [];
  const open = [...list].reverse().find((message) => message.role === "assistant" && !message.done && !message.error);
  if (open) {
    return {
      messagesBySession: {
        ...state.messagesBySession,
        [sessionId]: reducer(list, open.id),
      },
    };
  }
  if (options?.createPlaceholder) {
    const pendingId = `${sessionId}:pending:${Date.now()}`;
    const next = [...list, { id: pendingId, sessionId, role: "assistant", content: "", done: false }];
    return {
      messagesBySession: {
        ...state.messagesBySession,
        [sessionId]: reducer(next, pendingId),
      },
    };
  }
  return {};
}

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      sessions: [],
      sessionsLoaded: false,
      activeSessionId: null,
      activeProjectId: "",
      pendingSessionOrigin: "",
      pinnedSessionIds: [],
      sessionActivity: {},
      sessionTerminals: {},
      dismissedProjectSessions: {},
      messagesBySession: {},
      pendingSendsBySession: {},
      draftAttachments: [],
      draftReferences: [],
      streams: {},
      interactions: {},
      notifications: [],
      stagedRevert: null,
      tasksBySession: {},
      roundsBySession: {},
      employeeProgress: {},
      employeeProgressText: {},
      employeeProgressDone: {},

      setSessions: (sessions) => set({ sessions, sessionsLoaded: true }),

      upsertSession: (session) =>
        set((state) => {
          const exists = state.sessions.some((item) => item.id === session.id);
          return {
            sessions: exists
              ? state.sessions.map((item) =>
                  item.id === session.id ? { ...item, ...session } : item,
                )
              : [session, ...state.sessions],
          };
        }),

      removeSessionLocal: (sessionId) =>
        set((state) => {
          const { [sessionId]: _removed, ...rest } = state.messagesBySession;
          // 任务台账/挂起交互随会话一并回收（防 map 键常驻增长、会话重建后读到旧条目）
          const { [sessionId]: _tasks, ...restTasks } = state.tasksBySession;
          const { [sessionId]: _round, ...restRounds } = state.roundsBySession;
          const { [sessionId]: _pendingSends, ...restPendingSends } =
            state.pendingSendsBySession;
          const restInteractions = Object.fromEntries(
            Object.entries(state.interactions).filter(
              ([, frame]) => frame.session_id !== sessionId,
            ),
          );
          return {
            sessions: state.sessions.filter((item) => item.id !== sessionId),
            messagesBySession: rest,
            tasksBySession: restTasks,
            roundsBySession: restRounds,
            pendingSendsBySession: restPendingSends,
            interactions: restInteractions,
            activeSessionId: state.activeSessionId === sessionId ? null : state.activeSessionId,
            streams: Object.fromEntries(
              Object.entries(state.streams).filter(([sid]) => sid !== sessionId),
            ),
            pinnedSessionIds: state.pinnedSessionIds.filter((id) => id !== sessionId),
            sessionActivity: Object.fromEntries(
              Object.entries(state.sessionActivity).filter(([key]) => key !== sessionId),
            ),
          };
        }),

      setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),

      setActiveProject: (projectId) => set({ activeProjectId: projectId }),

      setPendingSessionOrigin: (origin) => set({ pendingSessionOrigin: origin }),

      togglePin: (sessionId) =>
        set((state) => ({
          pinnedSessionIds: state.pinnedSessionIds.includes(sessionId)
            ? state.pinnedSessionIds.filter((id) => id !== sessionId)
            : [sessionId, ...state.pinnedSessionIds],
        })),

      markSessionActivity: (sessionId, state) =>
        set((current) => {
          const next = { ...current.sessionActivity };
          if (state === "idle") {
            delete next[sessionId];
          } else {
            next[sessionId] = state;
          }
          return { sessionActivity: next };
        }),

      clearSessionActivity: (sessionId) =>
        set((state) => {
          if (!(sessionId in state.sessionActivity)) return state;
          const next = { ...state.sessionActivity };
          delete next[sessionId];
          return { sessionActivity: next };
        }),

      // 记录每会话最近一轮终态——中断横幅按原因分档（模型不可用/超时/安全拦截…），
      // 不再一律显示「因网络或模型原因中断了」
      markSessionTerminal: (sessionId, terminalReason) =>
        set((state) => ({
          sessionTerminals: { ...state.sessionTerminals, [sessionId]: terminalReason },
        })),

      /** 项目移出列表后，其下旧会话不再出现在任何会话列表（本地隐藏位）。
       * 只记 id 快照——重新加入该项目时新建的会话不在快照内，照常显示。 */
      dismissProjectSessions: (projectId, sessionIds) =>
        set((state) => {
          if (sessionIds.length === 0) return state;
          const hidden = new Set(state.dismissedProjectSessions[projectId] ?? []);
          return {
            dismissedProjectSessions: {
              ...state.dismissedProjectSessions,
              [projectId]: [...hidden, ...sessionIds.filter((id) => !hidden.has(id))],
            },
          };
        }),

      loadMessages: (sessionId, messages) =>
        set((state) => {
          const existing = state.messagesBySession[sessionId] ?? [];
          // 流式中不覆盖 pending 槽（历史读回与实时流合并：保留本地未定稿尾部）
          const pending = existing.filter((message) => !message.done);
          const merged = [...messages, ...pending];
          return {
            messagesBySession: { ...state.messagesBySession, [sessionId]: merged },
          };
        }),

      appendSystemNote: (sessionId, content) =>
        set((state) => {
          const list = state.messagesBySession[sessionId] ?? [];
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [sessionId]: [
                ...list,
                {
                  id: `sys:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`,
                  sessionId,
                  role: "system",
                  content,
                  done: true,
                  local: true,
                },
              ],
            },
          };
        }),

      // 功能：员工回复落位后清掉本地系统过渡注记（「已转交团队员工，等待接单执行…」）
      clearTransientNotes: (sessionId) =>
        set((state) => {
          const list = state.messagesBySession[sessionId] ?? [];
          const next = list.filter(
            (message) => !(message.role === "system" && message.local === true),
          );
          if (next.length === list.length) return {};
          return {
            messagesBySession: { ...state.messagesBySession, [sessionId]: next },
          };
        }),

      appendEmployeeReply: (sessionId, reply) =>
        set((state) => {
          const list = state.messagesBySession[sessionId] ?? [];
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [sessionId]: [
                ...list,
                {
                  id: `emp:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`,
                  sessionId,
                  role: "assistant",
                  content: reply.content,
                  done: true,
                  local: true,
                  senderName: reply.name,
                  senderRole: reply.roleId,
                  // 回复时间戳（HH:mm 展示数据源；本地注入以当前时刻标记）
                  createdAt: new Date().toISOString(),
                  // 员工执行过程随回复固化（消息上方过程块；无过程不写键）
                  ...(reply.process?.length ? { process: reply.process } : {}),
                  // 成员任务卡关联键（该回复派发的子卡以 parent_id 匹配本值）
                  ...(reply.workItemId ? { workItemId: reply.workItemId } : {}),
                },
              ],
            },
          };
        }),

      appendLocalUser: (sessionId, content, attachments) => {
        const pendingId = `${sessionId}:pending:${Date.now()}`;
        set((state) => {
          const list = state.messagesBySession[sessionId] ?? [];
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [sessionId]: [
                ...list,
                {
                  id: `${pendingId}:user`,
                  sessionId,
                  role: "user",
                  content,
                  done: true,
                  local: true,
                  ...(attachments?.length ? { attachments } : {}),
                },
                { id: pendingId, sessionId, role: "assistant", content: "", done: false },
              ],
            },
            streams: { ...state.streams, [sessionId]: { sessionId, pendingId } },
          };
        });
        return pendingId;
      },

      enqueuePendingSend: (sessionId, text, attachments = []) => {
        const content = text.trim();
        if (!content && attachments.length === 0) return "";
        const id = `pending-send:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
        set((state) => ({
          pendingSendsBySession: {
            ...state.pendingSendsBySession,
            [sessionId]: [
              ...(state.pendingSendsBySession[sessionId] ?? []),
              {
                id,
                text: content,
                attachments: [...attachments],
                createdAt: new Date().toISOString(),
              },
            ],
          },
        }));
        return id;
      },

      removePendingSend: (sessionId, id) =>
        set((state) => {
          const next = (state.pendingSendsBySession[sessionId] ?? []).filter(
            (item) => item.id !== id,
          );
          const pendingSendsBySession = { ...state.pendingSendsBySession };
          if (next.length > 0) pendingSendsBySession[sessionId] = next;
          else delete pendingSendsBySession[sessionId];
          return { pendingSendsBySession };
        }),

      clearPendingSends: (sessionId) =>
        set((state) => {
          const pendingSendsBySession = { ...state.pendingSendsBySession };
          delete pendingSendsBySession[sessionId];
          return { pendingSendsBySession };
        }),

      addDraftAttachment: (item) =>
        set((state) => ({ draftAttachments: [...state.draftAttachments, item] })),

      updateDraftAttachment: (uploadId, patch) =>
        set((state) => ({
          draftAttachments: state.draftAttachments.map((item) =>
            item.upload_id === uploadId ? { ...item, ...patch } : item,
          ),
        })),

      removeDraftAttachment: (uploadId) =>
        set((state) => ({
          draftAttachments: state.draftAttachments.filter((item) => item.upload_id !== uploadId),
        })),

      clearDraftAttachments: () => set({ draftAttachments: [] }),

      clearSentAttachments: () =>
        set((state) => ({
          draftAttachments: state.draftAttachments.filter((item) => item.status !== "done"),
        })),

      addDraftReference: (item) =>
        set((state) => {
          // 同路径去重（重复引用不叠加 chips）
          if (state.draftReferences.some((ref) => ref.path === item.path)) return {};
          return { draftReferences: [...state.draftReferences, item] };
        }),

      removeDraftReference: (path) =>
        set((state) => ({
          draftReferences: state.draftReferences.filter((ref) => ref.path !== path),
        })),

      clearDraftReferences: () => set({ draftReferences: [] }),

      appendDelta: (sessionId, text, options) =>
        set((state) =>
          updatePending(
            state,
            sessionId,
            (list, pendingId) => applyDelta(list, pendingId, text),
            options,
          ),
        ),

      applyThinking: (sessionId, text, options) =>
        set((state) =>
          updatePending(
            state,
            sessionId,
            (list, pendingId) => applyThinking(list, pendingId, text),
            options,
          ),
        ),

      applyNote: (sessionId, noteType) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applyNote(list, pendingId, noteType),
          ),
        ),

      applySubagentProgress: (sessionId, subagent, kind, payload) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applySubagentProgress(list, pendingId, subagent, kind, payload),
          ),
        ),

      applyMemoryWritten: (sessionId, channels) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applyMemoryWritten(list, pendingId, channels),
          ),
        ),

      setTaskLedger: (sessionId, items) =>
        set((state) => ({
          tasksBySession: { ...state.tasksBySession, [sessionId]: items },
        })),

      applyRound: (round) =>
        set((state) => ({
          roundsBySession: { ...state.roundsBySession, [round.session_id]: round },
        })),

      clearRound: (sessionId) =>
        set((state) => {
          if (!(sessionId in state.roundsBySession)) return {};
          const { [sessionId]: _removed, ...rest } = state.roundsBySession;
          return { roundsBySession: rest };
        }),

      applyEmployeeProgress: (projectId, threadId, roleId, event) =>
        set((state) => {
          const key = progressKey(projectId, threadId, roleId);
          const finished = state.employeeProgressDone[key] === true;
          // 终态帧在「已定稿」的槽上幂等忽略——终态会因为补发/重复送达
          // 再次到达，它**不代表「新执行开始」**，不能把已有过程块（含断线结算注记）清空重建。
          // （补发路径：断线期间结束的那次执行，重连后 terminal 会随回放再来一次。）
          const terminal = event.type === "done" || event.type === "error";
          if (finished && terminal) return {};
          // 上一执行已完成（done 标记）+ 非终态新过程帧 = 新执行开始 → 清空重建
          // （过程块保留可回顾，直到下一次执行覆盖或页面刷新）
          const current = finished ? [] : (state.employeeProgress[key] ?? []);
          const baseText = finished ? "" : (state.employeeProgressText[key] ?? "");
          const nextText =
            event.type === "content_delta"
              ? `${baseText}${String(event.data?.text ?? "")}`
              : baseText;
          const nextDone = finished
            ? Object.fromEntries(
                Object.entries(state.employeeProgressDone).filter(([itemKey]) => itemKey !== key),
              )
            : state.employeeProgressDone;
          return {
            employeeProgress: {
              ...state.employeeProgress,
              [key]: applyProgressNode(current, event),
            },
            employeeProgressText: {
              ...state.employeeProgressText,
              [key]: nextText,
            },
            employeeProgressDone: nextDone,
          };
        }),

      finishEmployeeProgress: (projectId, threadId, roleId) =>
        set((state) => {
          const key = progressKey(projectId, threadId, roleId);
          const current = state.employeeProgress[key] ?? [];
          return {
            // 收敛仍 running 的过程节点为 success（完成态展示；与消息级 finalizeMessage 同语义）
            employeeProgress: {
              ...state.employeeProgress,
              [key]: settleRunningNodes(current),
            },
            employeeProgressDone: {
              ...state.employeeProgressDone,
              [key]: true,
            },
          };
        }),

      consumeEmployeeProgress: (projectId, threadId, roleId) => {
        const key = progressKey(projectId, threadId, roleId);
        const nodes = get().employeeProgress[key] ?? [];
        if (nodes.length === 0) return nodes;
        set((state) => {
          const { [key]: _taken, ...rest } = state.employeeProgress;
          const { [key]: _done, ...restDone } = state.employeeProgressDone;
          const { [key]: _text, ...restText } = state.employeeProgressText;
          return {
            employeeProgress: rest,
            employeeProgressDone: restDone,
            employeeProgressText: restText,
          };
        });
        return nodes;
      },

      clearEmployeeProgress: (projectId, threadId, roleId) =>
        set((state) => {
          const key = progressKey(projectId, threadId, roleId);
          if (!(key in state.employeeProgress) && !(key in state.employeeProgressDone)) return {};
          const { [key]: _cleared, ...rest } = state.employeeProgress;
          const { [key]: _done, ...restDone } = state.employeeProgressDone;
          const { [key]: _text, ...restText } = state.employeeProgressText;
          return {
            employeeProgress: rest,
            employeeProgressDone: restDone,
            employeeProgressText: restText,
          };
        }),

      // 兜底过期——只在「已完成」标记下清槽（新执行已开始时标记已被重置，不动）
      expireEmployeeProgressIfDone: (projectId, threadId, roleId) =>
        get().expireEmployeeProgressByKey(progressKey(projectId, threadId, roleId)),

      // 同上，按槽键直接清（断线结算返回键，避免拆键再拼键）
      expireEmployeeProgressByKey: (key) =>
        set((state) => {
          if (state.employeeProgressDone[key] !== true) return {};
          const { [key]: _cleared, ...rest } = state.employeeProgress;
          const { [key]: _done, ...restDone } = state.employeeProgressDone;
          const { [key]: _text, ...restText } = state.employeeProgressText;
          return {
            employeeProgress: rest,
            employeeProgressDone: restDone,
            employeeProgressText: restText,
          };
        }),

      // 断线结算——仍在 running 的槽收敛为完成态 + 附注（已完成的槽/空槽不动）
      settleStaleEmployeeProgress: () => {
        const settled: string[] = [];
        set((state) => {
          const progress = { ...state.employeeProgress };
          const done = { ...state.employeeProgressDone };
          for (const [key, nodes] of Object.entries(state.employeeProgress)) {
            if (state.employeeProgressDone[key] === true || nodes.length === 0) continue;
            progress[key] = [
              ...settleRunningNodes(nodes),
              {
                id: `${key}:note:${nodes.length + 1}`,
                kind: "note" as const,
                noteType: "disconnected",
                status: "success" as const,
              },
            ];
            done[key] = true;
            // 返回槽键本身（host 直接用它挂兜底回收，不再拆键拼键——键现在有三段）
            settled.push(key);
          }
          return { employeeProgress: progress, employeeProgressDone: done };
        });
        return settled;
      },

      applySkillInjected: (sessionId, skills) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applySkillInjected(list, pendingId, skills),
          ),
        ),

      applyToolStart: (sessionId, toolName, toolInput, toolCallId) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applyToolStart(list, pendingId, toolName, toolInput, toolCallId),
          ),
        ),

      applyToolProgress: (sessionId, event) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applyToolProgress(list, pendingId, event),
          ),
        ),

      applyToolResult: (sessionId, event) =>
        set((state) =>
          updatePending(state, sessionId, (list, pendingId) =>
            applyToolResult(list, pendingId, event),
          ),
        ),

      // -3b：失败工具跳过（本地状态）与节点级重试结果回填（不接回 Agent 循环）
      skipToolNode: (sessionId, nodeId) =>
        set((state) => {
          const list = state.messagesBySession[sessionId] ?? [];
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [sessionId]: markToolSkipped(list, nodeId),
            },
          };
        }),

      applyToolRetry: (sessionId, toolCallId, result) =>
        set((state) => {
          const list = state.messagesBySession[sessionId] ?? [];
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [sessionId]: applyToolRetryResult(list, toolCallId, result),
            },
          };
        }),

      finalizeStream: (sessionId, patch) =>
        set((state) => {
          const slot = state.streams[sessionId];
          if (slot) {
            const list = state.messagesBySession[sessionId] ?? [];
            const nextStreams = { ...state.streams };
            delete nextStreams[sessionId];
            return {
              messagesBySession: {
                ...state.messagesBySession,
                [sessionId]: finalizeMessage(list, slot.pendingId, patch),
              },
              streams: nextStreams,
            };
          }
          // 跨槽/无槽终态帧（多轮回合后续 turn）：定稿该会话最后一条未定稿消息
          const list = state.messagesBySession[sessionId] ?? [];
          const open = [...list]
            .reverse()
            .find((message) => message.role === "assistant" && !message.done && !message.error);
          if (!open) return {};
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [sessionId]: finalizeMessage(list, open.id, patch),
            },
          };
        }),

      failStream: (sessionId, error) => {
        const { messagesBySession, streams } = get();
        const list = messagesBySession[sessionId] ?? [];
        const slot = streams[sessionId];
        const pendingId =
          slot?.pendingId ??
          ([...list]
            .reverse()
            .find((message) => message.role === "assistant" && !message.done && !message.error)
            ?.id ?? "");
        if (!pendingId) return;
        const nextStreams = { ...streams };
        delete nextStreams[sessionId];
        set({
          messagesBySession: {
            ...messagesBySession,
            [sessionId]: finalizeMessage(list, pendingId, { error }),
          },
          streams: nextStreams,
        });
      },

      // 停止后「正在回复中」残留兜底——后端无活动流（abort 404）
      // 或超时未见终结帧时，本地把挂起消息收尾：空占位（无内容无过程）直接移除
      // 有内容定稿为 done（保留已产出的部分）
      abortLocalStream: (sessionId) =>
        set((state) => {
          const slot = state.streams[sessionId];
          if (!slot) return {};
          const list = state.messagesBySession[slot.sessionId] ?? [];
          const pending = list.find((message) => message.id === slot.pendingId);
          const isEmptyPlaceholder =
            pending !== undefined &&
            !pending.content.trim() &&
            (pending.process?.length ?? 0) === 0;
          const nextStreams = { ...state.streams };
          delete nextStreams[slot.sessionId];
          return {
            messagesBySession: {
              ...state.messagesBySession,
              [slot.sessionId]: isEmptyPlaceholder
                ? list.filter((message) => message.id !== slot.pendingId)
                : finalizeMessage(list, slot.pendingId, {}),
            },
            streams: nextStreams,
          };
        }),

      addInteraction: (frame) =>
        set((state) => ({
          interactions: { ...state.interactions, [frame.interaction_id]: frame },
        })),

      resolveInteraction: (interactionId) =>
        set((state) => {
          const { [interactionId]: _resolved, ...rest } = state.interactions;
          return { interactions: rest };
        }),

      pushNotification: (item) =>
        set((state) => ({
          notifications: [
            {
              id: item.id ?? `n:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`,
              kind: item.kind,
              title: item.title,
              body: item.body,
              sessionId: item.sessionId,
              ts: Date.now(),
              read: false,
            },
            ...state.notifications,
          ].slice(0, NOTIFICATION_CAP),
        })),

      markNotificationsRead: () =>
        set((state) => ({
          notifications: state.notifications.map((item) => ({ ...item, read: true })),
        })),

      clearNotifications: () => set({ notifications: [] }),

      setStagedRevert: (staged) => set({ stagedRevert: staged }),

      setFeedback: async (sessionId, messageId, value) => {
        const list = get().messagesBySession[sessionId] ?? [];
        const target = list.find((message) => message.id === messageId);
        const entryId = target?.entryId;
        if (!target || !entryId) return;
        const previous = target.feedback;
        const apply = (next: "up" | undefined) =>
          set((state) => {
            const current = state.messagesBySession[sessionId] ?? [];
            return {
              messagesBySession: {
                ...state.messagesBySession,
                [sessionId]: current.map((message) =>
                  message.id === messageId ? { ...message, feedback: next } : message,
                ),
              },
            };
          });
        apply(value ?? undefined);
        try {
          await sessionsApi.setEntryFeedback(sessionId, entryId, value);
        } catch {
          apply(previous);
          throw new Error("点赞保存失败，已回滚");
        }
      },
    }),
    {
      name: "bandi:chat",
      partialize: (state) => ({
        activeSessionId: state.activeSessionId,
        activeProjectId: state.activeProjectId,
        pinnedSessionIds: state.pinnedSessionIds,
        sessionTerminals: state.sessionTerminals,
        dismissedProjectSessions: state.dismissedProjectSessions,
      }),
    },
  ),
);

// 功能：dev 专用钩子（生产构建被 import.meta.env.DEV 静态剔除，不进产物）。
// 用途：多张决策卡并存无法由生产链路自然产生（request_user_input 阻塞等待且
// is_concurrency_safe=false，同一时刻只会有一张卡），该路径需向真实
// store 注入第二张交互帧。生产代码无 window 暴露，故在此开 dev 口子。
if (import.meta.env.DEV && typeof window !== "undefined") {
  (window as unknown as { __bandiChatStore?: typeof useChatStore }).__bandiChatStore = useChatStore;
}
