/**
 * 功能：公司域 API（@base docs/API契约.md；//端点消费）。
 * 覆盖：项目（含成员动态解析）/ 收件箱（按项目）/ 讨论线程（列表·详情）/ 数字员工列表。
 */
import { api } from "@/services/api";

export interface ProjectMember {
  team_id: string;
  role_id: string;
  role_name: string;
  employee_id: string;
  employee_name: string;
  employee_status: string;
}

export interface ProjectRecord {
  project_id: string;
  name: string;
  root_dir: string;
  team_ids: string[];
  dissolved_team_ids?: string[];
  created_at: string;
  updated_at: string;
  members: ProjectMember[];
}

export interface InboxMessage {
  message_id: string;
  sender_role: string;
  content: string;
  created_at: string;
}

export interface ThreadSummary {
  thread_id: string;
  parent_task_id: string | null;
  participants: string[];
  created_at: string;
  message_count: number;
  /** 群聊会话窗口归属（群聊线程=会话 id；单聊为空）。 */
  session_id?: string;
  /** 线程类别：group=群聊窗口 / dm=员工单聊窗口 / 空=旧数据。 */
  kind?: string;
}

export interface ThreadMessageRecord {
  sender: string;
  content: string;
  mentions: string[];
  created_at: string;
  message_id: string;
  /** 员工执行过程事件快照（后端回写持久化；刷新后过程块回放数据源）。 */
  process_events?: unknown[];
  /** 线程消息附件（用户上传图片等；历史与实时帧同源）。 */
  metadata?: {
    attachments?: { upload_id: string; name: string; size: number; project_id?: string }[];
  };
}

export interface ThreadDetail {
  thread_id: string;
  parent_task_id: string | null;
  participants: string[];
  created_at: string;
  messages: ThreadMessageRecord[];
}

export interface EmployeeSummary {
  employee_id: string;
  name: string;
  role_id: string;
  description?: string;
  status?: string;
  /** 员工扩展信息（岗位人设 / 头像 / 团队来源）。 */
  metadata?: Record<string, unknown>;
  /** 档案 general_skill 绑定（实例化时按角色写入；右栏技能面按此过滤）。 */
  skills?: string[];
  /** 员工独立模型配置（换大脑）——default_model 存在即该员工用自己的模型
   * 不随全局激活档案变化；缺省/空 = 跟随全局。右栏控制条据此显示并锁定切换。 */
  llm_config?: {
    default_model?: string;
    base_url?: string;
    thinking_mode?: string;
  } | null;
}

export interface WorkItemSummary {
  id: string;
  title: string;
  description?: string;
  phase: string;
  /** 回合类型：execute=执行 / review=评审（评审回合落 gate 卡）。 */
  turn_type?: string;
  assignee_role: string;
  column?: string;
  status?: string;
  /** 父工作项（工作项树层级——后端 dict 提供）。 */
  parent_id?: string | null;
  /** 创建时间（fallback 运行态过滤：回复落位后旧卡不得复活）。 */
  created_at?: string;
  /** 最近更新时间（计划区排序用——后端 dict 提供）。 */
  updated_at?: string;
  /** 项目归属（后端补字段；旧卡为空串）。 */
  project_id?: string;
  /** 创建该任务的来源会话；群聊计划按它做会话隔离。 */
  origin_session_id?: string;
  /** 绑定线程（单聊视图据此只统计本会话的卡）。 */
  thread_id?: string;
  /** 员工产出预览（_last_output 截断；计划树行内摘要展示）。 */
  last_output_preview?: string;
  /** 是否带评审门（看板卡片徽标）。 */
  has_gate?: boolean;
  /** 依赖条数（看板卡片徽标）。 */
  dependency_count?: number;
  /** 数据来源（chat_mention/chat_dm=会话卡；agent_to_agent=分工任务）。计划/任务卡过滤依据。 */
  source?: string;
  /** 意图标记（四态）：query=咨询问答（不出发计划）/ confirm=确认执行 / adjust=调整。 */
  chat_kind?: string;
  /** 建卡时会话模式（plan/execute）。 */
  created_mode?: string;
  /** plan 模式门：非空=等待用户切执行模式后才会执行。 */
  mode_gate?: string;
}

/** 团队模板摘要（GET /api/company/teams；选择团队弹窗数据源）。 */
export interface TeamTemplateRole {
  id: string;
  name: string;
  responsibility?: string;
  reports_to?: string | null;
  can_spawn?: string[];
  tool_tags?: string[];
  is_final_decider?: boolean;
}

export interface TeamTemplateSummary {
  template_id: string;
  name: string;
  description?: string;
  team_id?: string;
  roles?: TeamTemplateRole[];
  role_count?: number;
  created_at?: string;
}

/** 已配置团队摘要（GET /api/company/org-teams；选择团队弹窗数据源）。 */
export interface OrgTeamSummary {
  team_id: string;
  name: string;
  role_count: number;
  employee_count: number;
  roles: Array<{ role_id: string; name: string }>;
}

export interface WorkItemsResult {
  items: WorkItemSummary[];
  column_counts: Record<string, number>;
  /** hide_chat 默认开启时被隐藏的纯对话卡数量（UI 显示「已隐藏 N 张对话卡」）。 */
  hidden_chat_count?: number;
}

/** 群聊回合席位（Round 状态条数据源） */
export interface GroupRoundSlot {
  slot_id: string;
  role_id: string;
  role_name: string;
  status: string;
  origin: string;
  called_by: string;
  order: number;
  work_item_id: string;
}

/** 群聊回合快照（GET /api/company/rounds 与 group_round 帧同构） */
export interface GroupGoalSnapshot {
  goal_id: string;
  session_id: string;
  project_id: string;
  title: string;
  status: string;
  current: number;
  total: number;
  stage_title: string;
  owner_role_id: string;
  owner_name: string;
  revision: number;
  updated_at: number;
}

export interface GroupRoundSnapshot {
  round_id: string;
  session_id: string;
  project_id: string;
  status: string;
  relay_count: number;
  relay_limit: number;
  handoff_note?: string;
  slots: GroupRoundSlot[];
  updated_at?: number;
}

/** 附件上传记录（端点返回；Day137b-2 前端消费）。 */
export interface UploadRecord {
  upload_id: string;
  name: string;
  size: number;
  project_id: string;
  path: string;
}

/** 项目文件条目（@文件引用数据源——Day137b-2）。 */
export interface ProjectFile {
  path: string;
  name: string;
  is_dir: boolean;
}

/** 本地目录条目（「打开本地文件夹」选择器——）。 */
export interface DirectoryEntry {
  name: string;
  path: string;
}

/** 本地目录列举结果（path 空 + is_root=true 时 entries = 盘符）。 */
export interface DirectoryListing {
  path: string;
  parent: string;
  entries: DirectoryEntry[];
  is_root: boolean;
}

/** 新建项目空间入参（create_folder=true → 本地自动建同名文件夹）。 */
export interface ProjectCreateInput {
  project_id: string;
  name: string;
  root_dir?: string;
  create_folder?: boolean;
}

export const companyApi = {
  projects: () => api.get<{ projects: ProjectRecord[] }>("/company/projects"),
  /** 功能：创建项目空间（create_folder=true → 本地自动建同名文件夹并登记为工作目录）。 */
  createProject: (input: ProjectCreateInput) =>
    api.post<{ project: ProjectRecord }>("/company/projects", input),
  /** 功能：本地目录列举（内置选择器回落用；path 空 → 盘符/主目录）。 */
  directories: (path = "") =>
    api.get<DirectoryListing>(
      `/company/fs/directories${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  /** 功能：调系统原生文件夹选择框（真实桌面弹窗）；fallback=true 表示原生不可用需回落内置选择器。 */
  pickDirectory: () =>
    api.post<{ path: string; fallback: boolean }>("/company/fs/pick-directory", {}),
  inbox: (roleId: string, projectId = "") =>
    api.get<InboxMessage[]>(
      `/company/inbox?role_id=${encodeURIComponent(roleId)}${
        projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""
      }`,
    ),
  threads: (projectId = "") =>
    api.get<{ threads: ThreadSummary[] }>(
      projectId
        ? `/company/threads?project_id=${encodeURIComponent(projectId)}`
        : "/company/threads",
    ),
  thread: (threadId: string, projectId = "") =>
    api.get<ThreadDetail>(
      projectId
        ? `/company/threads/${threadId}?project_id=${encodeURIComponent(projectId)}`
        : `/company/threads/${threadId}`,
    ),
  /** 功能：删除讨论线程（员工单聊窗口）——删除后新线程新上下文。 */
  deleteThread: (threadId: string, projectId = "") =>
    api.del<{ deleted: boolean; thread_id: string }>(
      projectId
        ? `/company/threads/${threadId}?project_id=${encodeURIComponent(projectId)}`
        : `/company/threads/${threadId}`,
    ),
  /** 功能：持久化追加线程系统提示（同 dedupe_key / 同内容只保留一条）。 */
  appendThreadSystemNote: (
    threadId: string,
    content: string,
    noteKind: string,
    projectId = "",
    dedupeKey = "",
  ) =>
    api.post<{ created: boolean; thread_id: string }>(
      `/company/threads/${threadId}/system-notes${
        projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""
      }`,
      { content, note_kind: noteKind, ...(dedupeKey ? { dedupe_key: dedupeKey } : {}) },
    ),
  /** 功能：批量查询墓碑（只返回命中身份）。 */
  employeeTombstones: (ids: string[]) =>
    api.post<{
      tombstones: Record<string, { employee_id: string; role_id: string; name: string }>;
    }>("/company/employee-tombstones/lookup", { ids }),
  /** 功能：查询硬删除员工/岗位的墓碑；不存在返回 null。 */
  employeeTombstone: (identityId: string) =>
    api
      .get<{ tombstone: { employee_id: string; role_id: string; name: string } }>(
        `/company/employee-tombstones/${encodeURIComponent(identityId)}`,
      )
      .catch(() => null),
  /** 功能：员工列表；includeInactive=true 时含下线与退役员工。 */
  employees: (includeInactive = false) =>
    api.get<{ employees: EmployeeSummary[] }>(
      `/company/employees${includeInactive ? "?include_inactive=true" : ""}`,
    ),
  /** 功能：切换会话模式（落库 sessions.mode；execute 时后端同时释放 plan 门挂起的派发卡）。 */
  setChatMode: (sessionId: string, mode: "execute" | "plan") =>
    api.post<{ released: number; mode: string }>("/company/chat/mode", {
      session_id: sessionId,
      mode,
    }),
  /** 功能：切换会话到执行模式——释放该会话下 plan 门挂起的派发卡。 */
  releaseModeGate: (sessionId: string) =>
    api.post<{ released: number; mode: string }>("/company/chat/mode", {
      session_id: sessionId,
      mode: "execute",
    }),
  /** 功能：团队模板列表（；选择团队弹窗数据源）。 */
  teams: () => api.get<{ teams: TeamTemplateSummary[] }>("/company/teams"),
  /** 功能：已配置团队清单（org team_id 聚合；选择团队弹窗数据源）。 */
  orgTeams: () => api.get<{ teams: OrgTeamSummary[] }>("/company/org-teams"),  /** 功能：实例化团队模板（；幂等重入，已存在角色跳过）。 */
  instantiateTeam: (templateId: string) =>
    api.post<{ report: Record<string, unknown> }>(
      `/company/teams/${encodeURIComponent(templateId)}/instantiate`,
      { team_id: "" },
    ),
  /** 功能：团队拉入项目（；未组建需先实例化——见 SelectTeamDialog）。 */
  addTeam: (projectId: string, teamId: string) =>
    api.post<{ project: ProjectRecord }>(
      `/company/projects/${encodeURIComponent(projectId)}/teams`,
      { team_id: teamId },
    ),
  /** 功能：组织角色列表（工作项岗位 id → 展示名映射）。 */
  roles: () => api.get<{ roles: Array<{ id: string; name: string }> }>("/company/roles"),
  /** 功能：WorkItem 查询（hideChat=false 时连纯对话卡一起返回）。 */
  workItems: (role = "", hideChat = true) => {
    const params = new URLSearchParams();
    if (role) params.set("role", role);
    if (!hideChat) params.set("hide_chat", "false");
    const query = params.toString();
    return api.get<WorkItemsResult>(
      query ? `/company/work-items?${query}` : "/company/work-items",
    );
  },
  /** 功能：取会话当前运行/暂停中的群聊回合 */
  round: (sessionId: string, projectId = "") =>
    api.get<{ round: GroupRoundSnapshot | null }>(
      `/company/rounds?session_id=${encodeURIComponent(sessionId)}${
        projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""
      }`,
    ),
  /** 功能：取会话当前群聊目标摘要 */
  goal: (sessionId: string) =>
    api.get<{ goal: GroupGoalSnapshot | null }>(
      `/company/goals?session_id=${encodeURIComponent(sessionId)}`,
    ),
  /** 功能：恢复被接力上限暂停的群聊回合 */
  resumeRound: (roundId: string) =>
    api.post<{ round: GroupRoundSnapshot | null; work_item_id: string }>(
      `/company/rounds/${encodeURIComponent(roundId)}/resume`,
      {},
    ),
  /** 功能：取消群聊回合并中断已派发的卡 */
  cancelRound: (roundId: string) =>
    api.post<{ round: GroupRoundSnapshot | null; cancelled_work_items: string[] }>(
      `/company/rounds/${encodeURIComponent(roundId)}/cancel`,
      {},
    ),
  /** 功能：重试需关注的卡（仅 NEEDS_ATTENTION 有效；其他相位 400）。 */
  retryWorkItem: (workItemId: string) =>
    api.post<{ work_item_id: string; queued: boolean }>(
      `/company/work-items/${encodeURIComponent(workItemId)}/retry`,
      {},
    ),
  /** 功能：评审决策（verdict 只接受 approve / rework）。 */
  reviewWorkItem: (workItemId: string, verdict: "approve" | "rework", feedback = "") =>
    api.post<{ work_item_id: string }>(
      `/company/work-items/${encodeURIComponent(workItemId)}/review`,
      { verdict, feedback },
    ),
  /** 功能：中断执行中的工作项（；无活动执行 404=NO_ACTIVE_EXECUTION）。 */
  abortWorkItem: (workItemId: string) =>
    api.post<{ work_item_id: string; interrupted: boolean }>(
      `/company/work-items/${encodeURIComponent(workItemId)}/abort`,
      {},
    ),
  /** 功能：从列表删除项目（仅删项目记录；会话与文件不动）。 */
  deleteProject: (projectId: string) =>
    api.del<{ deleted: boolean }>(`/company/projects/${encodeURIComponent(projectId)}`),
  /** 功能：项目改名（仅改 name；project_id 不可变，其他字段不动）。 */
  renameProject: (projectId: string, name: string) =>
    api.patch<{ project: ProjectRecord }>(
      `/company/projects/${encodeURIComponent(projectId)}`,
      { name },
    ),
  /** 功能：在系统文件管理器中打开项目工作目录（未设置目录 400 / 不存在 404）。 */
  openFolder: (projectId: string) =>
    api.post<{ opened: string }>(`/company/projects/${encodeURIComponent(projectId)}/open-folder`),
  /** 功能：用系统默认程序打开本地文件/目录（越界 403）。 */
  /** 预留：/api/fs/open 的 API 绑定；启用时机 UI 需系统打开时（当前 UI 已改右栏文件页签）。 */
  openLocalPath: (path: string) => api.post<{ opened: string }>("/fs/open", { path }),
  /** 功能：对话附件上传（multipart；返回 upload_id 引用——端点，Day137b-2）。 */
  upload: (file: File, projectId = "") => {
    const form = new FormData();
    form.append("file", file);
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return api.postForm<{ upload: UploadRecord }>(`/company/uploads${qs}`, form);
  },
  /** 功能：附件下载 URL（`<a href>` / 图片预览直接使用）。 */
  uploadUrl: (uploadId: string, projectId = "") =>
    `/api/company/uploads/${encodeURIComponent(uploadId)}${
      projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""
    }`,
  /** 功能：项目文件列表（@文件引用数据源；root_dir 未设置回空列表）。 */
  files: (projectId: string, query = "") =>
    api.get<{ files: ProjectFile[]; root_dir: string }>(
      `/company/projects/${encodeURIComponent(projectId)}/files${
        query ? `?query=${encodeURIComponent(query)}` : ""
      }`,
    ),
};

/** 功能：找与目标员工最近的一条单聊线程（participants 集合匹配，取创建时间最新）。 */
export function findDirectThread(
  threads: ThreadSummary[],
  targetRoleId: string,
): ThreadSummary | null {
  const wanted = new Set(["user", targetRoleId]);
  const matches = threads.filter((thread) => {
    const participants = new Set(thread.participants);
    return participants.size === wanted.size && [...wanted].every((role) => participants.has(role));
  });
  if (matches.length === 0) return null;
  // 最新优先：按 created_at 取最大（不依赖后端返回顺序；当前模型下每员工每项目恒单例
  // 多线程为未来扩展的语义准备）
  return matches.reduce((latest, thread) =>
    (thread.created_at ?? "") > (latest.created_at ?? "") ? thread : latest,
  );
}
