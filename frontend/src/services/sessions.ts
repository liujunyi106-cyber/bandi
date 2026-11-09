/**
 * 功能：会话域 API。
 * 覆盖：列表（含 project_id 项目维度）/ 详情（消息条目）/ 重命名 / 删除 /
 * fork / revert 三级（stage·commit·clear）/ 分享 + 共享查看 / 转向（steer·inject）/
 * 中断（abort）/ 挂起交互应答（confirm·clarify）。
 */
import { api } from "@/services/api";

/** 会话摘要（GET /api/sessions 条目）。 */
export interface SessionSummary {
  id: string;
  title: string;
  created_at: string;
  message_count: number;
  /** 项目维度（空串 = 无项目会话；两级分组数据源）。 */
  project_id: string;
  /** 来源标记（"secretary" = 秘书会话；"employee_dm" = 员工单聊；空 = 普通会话）。 */
  origin?: string;
  /** 员工单聊目标角色（普通/群聊会话为空）。 */
  target_role?: string;
  /** 员工单聊目标员工（普通/群聊会话为空）。 */
  employee_id?: string;
  /** 员工显示名（会话列表员工分组标题）。 */
  employee_name?: string;
  /** 会话当前绑定线程（线程消息按会话过滤）。 */
  thread_id?: string;
  /** 会话状态（"active" = 活跃；"archived" = 已归档）。 */
  status?: string;
}

/** 会话消息条目（session_entries 行）。 */
export interface SessionEntry {
  id: string;
  session_id?: string;
  parent_id?: string | null;
  entry_type: string;
  turn_index: number;
  role: string;
  content: string;
  tool_call_id?: string | null;
  metadata?: string;
  created_at?: string;
  /** 点赞标记（"up" = 已点赞；空 = 无）。刷新后由服务端带回。 */
  feedback?: string | null;
}

export interface SessionDetail {
  session: Record<string, unknown>;
  messages: SessionEntry[];
}

export interface SharedSession {
  session_id: string;
  messages: SessionEntry[];
}

/** 挂起交互帧载荷（WS interaction 帧 data）。 */
export interface InteractionFrame {
  interaction_id: string;
  session_id: string;
  kind: string;
  payload: Record<string, unknown>;
  /** 剩余倒计时秒数。 */
  expires_in?: number;
  /** 孤儿标记（重放端点标注：执行已结束、进程内无挂起方——答复无处唤醒）。 */
  orphaned?: boolean;
}

export const sessionsApi = {
  list: () => api.get<SessionSummary[]>("/sessions"),
  /** 功能：显式创建空会话（团队群聊条目点击即建——先落库再打开，选中态可持久）。 */
  create: (body: {
    title?: string;
    project_id?: string;
    origin?: string;
    target_role?: string;
    employee_id?: string;
    employee_name?: string;
  }) =>
    api.post<{
      session: {
        id: string;
        title?: string;
        created_at?: string;
        project_id?: string;
        origin?: string;
        target_role?: string;
        employee_id?: string;
        employee_name?: string;
      };
    }>("/sessions", body),
  detail: (sessionId: string, at?: string) =>
    api.get<SessionDetail>(
      at ? `/sessions/${sessionId}?at=${encodeURIComponent(at)}` : `/sessions/${sessionId}`,
    ),
  rename: (sessionId: string, title: string) =>
    api.patch<{ ok: boolean; title: string }>(`/sessions/${sessionId}`, { title }),
  remove: (sessionId: string) =>
    api.del<{ ok: boolean; thread_deleted?: boolean; group_memory_purged?: boolean }>(
      `/sessions/${sessionId}`,
    ),
  revertStage: (sessionId: string, entryId: string) =>
    api.post<{ staged: boolean }>(`/sessions/${sessionId}/revert/stage`, { entry_id: entryId }),
  revertCommit: (sessionId: string) =>
    api.post<{ committed: boolean; removed_entries: number; operation_id: string }>(
      `/sessions/${sessionId}/revert/commit`,
    ),
  revertUndo: (sessionId: string) =>
    api.post<{ restored_entries: number }>(`/sessions/${sessionId}/revert/undo`),
  revertClear: (sessionId: string) =>
    api.post<{ cleared: boolean }>(`/sessions/${sessionId}/revert/clear`),
  regenerate: (sessionId: string, entryId: string) =>
    api.post<{ session_id: string; entry_id: string; reply: string; terminal_reason: string }>(
      `/sessions/${sessionId}/regenerate`,
      { entry_id: entryId },
    ),
  share: (sessionId: string) => api.post<{ share_token: string }>(`/sessions/${sessionId}/share`),
  /** 功能：归档——项目会话沉淀摘要到项目 AGENTS.md；普通会话仅置状态不写文件。 */
  archive: (sessionId: string) =>
    api.post<{ summarized: boolean; distilled: boolean; archived: boolean; reason: string }>(
      `/sessions/${sessionId}/archive`,
    ),
  /** 功能：消息点赞标记（"up" 点赞 / null 取消）——落库并提名候选记忆。 */
  setEntryFeedback: (sessionId: string, entryId: string, feedback: "up" | null) =>
    api.patch<{ ok: boolean; entry_id: string; feedback: string | null; nominated: boolean }>(
      `/sessions/${sessionId}/entries/${encodeURIComponent(entryId)}`,
      { feedback },
    ),
  shared: (token: string) => api.get<SharedSession>(`/shared/${token}`),
  abort: (sessionId: string) => api.post<{ ok?: boolean }>(`/sessions/${sessionId}/abort`),
  /** P3：挂起交互重放——会话加载时补拉 pending 决策卡（刷新后卡不再丢失）。 */
  appendSystemNote: (
    sessionId: string,
    content: string,
    noteKind: string,
    dedupeKey: string,
  ) =>
    api.post<{ created: boolean; entry_id: string }>(`/sessions/${sessionId}/system-notes`, {
      content,
      note_kind: noteKind,
      dedupe_key: dedupeKey,
    }),
  pendingInteractions: (sessionId: string) =>
    api.get<{ interactions: InteractionFrame[] }>(`/sessions/${sessionId}/interactions`),
  steer: (sessionId: string, message: string) =>
    api.post<{ queued: boolean }>(`/sessions/${sessionId}/steer`, { message }),
  inject: (sessionId: string, message: string) =>
    api.post<{ injected: boolean }>(`/sessions/${sessionId}/inject`, { message }),
  /** 功能：强制压缩会话。 */
  compact: (sessionId: string) =>
    api.post<{
      compacted: boolean;
      new_session_id: string;
      stage: string;
      before: number;
      after: number;
      before_tokens: number;
      after_tokens: number;
      has_summary: boolean;
      reason: string;
    }>(`/sessions/${sessionId}/compact`),
  confirm: (
    sessionId: string,
    interactionId: string,
    decision: boolean,
    feedback?: string,
    /** 功能：授权作用域——once=仅本轮放行；
     * session=放行并落会话级授权，本会话内同工具同参不再重复询问。 */
    scope?: "once" | "session",
  ) =>
    api.post<{ outcome: string; scope?: string; session_granted?: string[] }>(
      `/sessions/${sessionId}/confirm`,
      {
        interaction_id: interactionId,
        decision,
        feedback: feedback ?? "",
        scope: scope ?? "once",
      },
    ),
  clarify: (sessionId: string, interactionId: string, answer: string) =>
    api.post<{ answered: boolean }>(`/sessions/${sessionId}/clarify`, {
      interaction_id: interactionId,
      answer,
    }),
  /** 功能：权限审批三态决策（allow_once/allow_session/deny；后端按会话取 pending
   * allow_session 触发同工具级联授权——；-3a）。 */
  approvalDecision: (
    sessionId: string,
    decision: "allow_once" | "allow_session" | "deny",
    feedback?: string,
    toolCallId?: string,
  ) =>
    api.post<{
      session_id: string;
      decision: string;
      tool_name: string;
      tool_call_id: string;
      reply_content: string;
    }>(`/sessions/${sessionId}/approval-decision`, {
      decision,
      feedback,
      ...(toolCallId ? { tool_call_id: toolCallId } : {}),
    }),
  /** 员工 headless 审批列表（ApprovalStore pending——数据桥数据源）。 */
  companyApprovals: () =>
    api.get<{
      approvals: Array<{
        session_id: string;
        tool_name: string;
        reason: string;
        tool_call_id: string;
        params_digest: string;
        detail?: string;
        prompt?: string;
        rule_pattern?: string;
        rule_source?: string;
      }>;
      count: number;
    }>("/company/approvals"),
  /** 功能：节点级工具重试（-3b）——按 tool_call_id force 真跑一次；
   * 结果只更新前端内存节点（不接回 Agent 循环）。 */
  retryTool: (sessionId: string, toolCallId: string) =>
    api.post<{
      tool_name: string;
      success: boolean;
      output_preview: string;
      error: string;
      duration_ms: number;
    }>(`/sessions/${sessionId}/retry-tool`, { tool_call_id: toolCallId }),
  /** 功能：跳过态随存（-3c）——按 tool_call_id 翻转已持久化 process 条目内节点。 */
  processSkip: (sessionId: string, toolCallId: string) =>
    api.post<{ ok: boolean }>(`/sessions/${sessionId}/process-skip`, {
      tool_call_id: toolCallId,
    }),
  /** 功能：会话产物清单（-3d 右栏制品预览 Tab；b 方案后端登记）。 */
  artifacts: (sessionId: string) =>
    api.get<{ session_id: string; artifacts: SessionArtifact[] }>(
      `/sessions/${sessionId}/artifacts`,
    ),
  /** 功能：当前轮上下文用量五类分桶（Composer 用量环与面板数据源）。 */
  contextUsage: (sessionId: string) =>
    api.get<ContextUsage>(`/sessions/${sessionId}/context-usage`),
  /** 功能：制品内容预览（-；安全面三条硬约束在后端——穿越/敏感/超大二进制 fail-closed）。 */
  artifactContent: (sessionId: string, artifactId: string, maxBytes = 65536) =>
    api.get<{
      artifact_id: string;
      path: string;
      size: number;
      truncated: boolean;
      kind: "text" | "binary" | "too_large";
      content: string;
      reason?: string;
    }>(
      `/sessions/${sessionId}/artifacts/${encodeURIComponent(artifactId)}/content?max_bytes=${maxBytes}`,
    ),
};

/** 会话产物条目（session_artifacts 行；-3d）。 */
export interface SessionArtifact {
  id: string;
  session_id: string;
  turn_index?: number;
  tool_name: string;
  artifact_type: string;
  path: string;
  hash: string;
  /** 关联工作项（员工回复下方交付物卡片精确匹配用）。 */
  work_item_id?: string;
  created_at?: string;
}

/** 上下文用量分类键（与后端 BUCKETS 顺序一致）。 */
export type ContextUsageBucketKey = "system" | "tools" | "messages" | "connectors" | "skills";

/** 单类上下文占用（tokens 为该类估算值，ratio = tokens / 模型窗口）。 */
export interface ContextUsageBucket {
  key: ContextUsageBucketKey;
  tokens: number;
  ratio: number;
}

/** 当前轮上下文用量（无快照时 available=false，窗口与百分比均为 0）。 */
export interface ContextUsage {
  session_id: string;
  available: boolean;
  model: string;
  context_window: number;
  used_tokens: number;
  ratio: number;
  buckets: ContextUsageBucket[];
  updated_at: string;
}

/**
 * 功能：项目→会话两级分组——按 project_id 分桶，无项目归空串组。
 * 保序：组按首个会话出现序（列表已按 updated_at 倒序），组内保持输入序。
 */
export function groupSessionsByProject(sessions: SessionSummary[]): Map<string, SessionSummary[]> {
  const groups = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    const key = session.project_id || "";
    const bucket = groups.get(key);
    if (bucket) bucket.push(session);
    else groups.set(key, [session]);
  }
  return groups;
}

/** 功能：员工单聊会话判定（origin 或 target_role 任一命中）。 */
export function isEmployeeDmSession(session: SessionSummary): boolean {
  return session.origin === "employee_dm" || Boolean(session.target_role);
}

/** 功能：员工单聊按目标角色分组（Map 保序：首个会话出现序）。 */
export function groupSessionsByEmployee(
  sessions: SessionSummary[],
): Map<string, SessionSummary[]> {
  const groups = new Map<string, SessionSummary[]>();
  for (const session of sessions) {
    if (!isEmployeeDmSession(session)) continue;
    const key = session.target_role || session.employee_id || "";
    if (!key) continue;
    const bucket = groups.get(key);
    if (bucket) bucket.push(session);
    else groups.set(key, [session]);
  }
  return groups;
}
