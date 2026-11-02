/**
  * 功能：会话右栏 v5 数据面（数字员工工作台；@base docs/前端设计规划.md）。
 * 文件树 / 文件预览 / 技能列表 / SOP 进度——四端点封装（后端已交付 `3a6fdce`）。
 * 安全口径：预览只读；路径沙箱由后端 fail-closed（越界/敏感 403、二进制/超大只回元数据）。
 */
import { api } from "@/services/api";

/** 文件树条目（GET /api/sessions/{sid}/files 或项目文件树）。 */
export interface FileTreeEntry {
  name: string;
  type: "dir" | "file";
  size: number;
  mtime: number;
}

export interface FileTreePayload {
  session_id?: string;
  project_id?: string;
  root: string;
  path: string;
  entries: FileTreeEntry[];
}

/** 文件预览（GET …/files/content；仅 kind=text 才有 content）。 */
export interface FileContentPayload {
  session_id?: string;
  project_id?: string;
  path: string;
  size: number;
  kind: "text" | "binary" | "too_large";
  content: string;
  truncated?: boolean;
  reason?: string;
}

/** 已安装技能（GET /api/skills）。 */
export interface InstalledSkill {
  name: string;
  description: string;
  tags: string[];
  team_id: string;
  always: boolean;
}

/** SOP 单帧进度（只读投影——不含槽位值）。 */
export interface SopFrameProgress {
  task_id: string;
  skill_id: string;
  node_id: string | null;
  status: string;
  awaiting_fields: string[];
  collected_fields: string[];
  updated_at: string;
}

/** SOP 帧栈进度（GET /api/sessions/{sid}/sop；无帧 = null）。 */
export interface SopProgress {
  scope_id: string;
  revision: number;
  active: SopFrameProgress | null;
  pending: SopFrameProgress[];
  completed_count: number;
  last_completed?: { task_id: string; skill_id: string; updated_at: string };
}

/** 会话 token 用量（GET /api/sessions/{sid}/cost-detail——概览摘要卡「token 用量」数据源）。 */
export interface CostDetail {
  session_id: string;
  total_tokens: number;
  per_model: Record<string, number>;
  tool_stats: Record<
    string,
    {
      calls: number;
      failures: number;
      total_ms: number;
      prompt_tokens: number;
      completion_tokens: number;
    }
  >;
}

/** 员工档案资源绑定（；resource_type ∈ skill/general_skill/knowledge_base/tool）。 */
export interface EmployeeProfileResource {
  resource_type: string;
  resource_id: string;
  status: string;
}

/** 员工能力档案（GET /api/company/employees/{id}/profile——右栏员工卡数据源）。 */
export interface EmployeeProfile {
  profile_id: string;
  employee_id: string;
  name: string;
  resources: EmployeeProfileResource[];
}

/** 员工个体知识库（GET /api/company/employees/{id}/knowledge——私有桶归属）。 */
export interface EmployeeKnowledgeBase {
  base_id: string;
  name: string;
  project_id: string;
  document_count: number;
}

/** 项目用量（GET /api/company/projects/{id}/usage——；role 空 = 团队口径）。 */
export interface ProjectUsage {
  project_id: string;
  role: string;
  /** 会话窗口（线程）口径；空 = 项目累计。 */
  thread_id?: string;
  total_tokens: number;
  total_cost_cny: number;
  requests: number;
}

/** 项目产物条目（GET /api/company/projects/{id}/artifacts——/聚合）。 */
export interface ProjectArtifact {
  id: string;
  session_id: string;
  path: string;
  tool_name: string;
  artifact_type?: string;
  hash?: string;
  /** 关联工作项（员工回复下方交付物卡片精确匹配用）。 */
  work_item_id?: string;
  created_at?: string;
}

export const panelApi = {
  /** 功能：项目文件树（懒加载——按 path 逐级展开；忽略名单由后端过滤）。 */
  files: (sessionId: string, path = "") =>
    api.get<FileTreePayload>(
      `/sessions/${sessionId}/files${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),
  /** 功能：项目文件内容预览（只读；二进制/超大时 content 为空、kind 标明原因）。 */
  fileContent: (sessionId: string, path: string, maxBytes = 65536) =>
    api.get<FileContentPayload>(
      `/sessions/${sessionId}/files/content?path=${encodeURIComponent(path)}&max_bytes=${maxBytes}`,
    ),
  /** 功能：项目文件树（员工作用域「文件」页签数据源——懒加载逐级展开）。 */
  projectFiles: (projectId: string, path = "") =>
    api.get<FileTreePayload>(
      `/company/projects/${encodeURIComponent(projectId)}/files/tree${
        path ? `?path=${encodeURIComponent(path)}` : ""
      }`,
    ),
  /** 功能：项目文件内容预览（只读；二进制/超大只回元数据）。 */
  projectFileContent: (projectId: string, path: string, maxBytes = 65536) =>
    api.get<FileContentPayload>(
      `/company/projects/${encodeURIComponent(projectId)}/files/content?path=${encodeURIComponent(
        path,
      )}&max_bytes=${maxBytes}`,
    ),
  /** 功能：项目用量（团队口径排除秘书；role 非空 = 该员工在本项目）。 */
  projectUsage: (projectId: string, role = "", threadId = "") => {
    const params = new URLSearchParams();
    if (role) params.set("role", role);
    if (threadId) params.set("thread_id", threadId);
    const query = params.toString();
    return api.get<ProjectUsage>(
      `/company/projects/${encodeURIComponent(projectId)}/usage${query ? `?${query}` : ""}`,
    );
  },
  /** 功能：项目产物聚合（团队 = 全量排除秘书；role 非空 = 该员工产物）。 */
  projectArtifacts: (projectId: string, role = "") =>
    api.get<{ project_id: string; role: string; artifacts: ProjectArtifact[]; count: number }>(
      `/company/projects/${encodeURIComponent(projectId)}/artifacts${
        role ? `?role=${encodeURIComponent(role)}` : ""
      }`,
    ),
  /** 功能：已安装技能列表（可选 team_id 过滤；数据源=SkillRegistry）。 */
  skills: (teamId = "") =>
    api.get<{ skills: InstalledSkill[] }>(
      `/skills${teamId ? `?team_id=${encodeURIComponent(teamId)}` : ""}`,
    ),
  /** 功能：SOP 进度（帧栈只读投影——与组织层 sop_progress 同源口径）。 */
  sop: (sessionId: string) =>
    api.get<{ session_id: string; sop: SopProgress | null }>(`/sessions/${sessionId}/sop`),
  /** 功能：会话用量详情（per-tool token 归因 + 会话级 token/成本）。 */
  costDetail: (sessionId: string) => api.get<CostDetail>(`/sessions/${sessionId}/cost-detail`),
  /** 功能：员工能力档案（知识库/技能绑定——只读查询；无档案 404）。 */
  employeeProfile: (employeeId: string) =>
    api.get<{ profile: EmployeeProfile }>(
      `/company/employees/${encodeURIComponent(employeeId)}/profile`,
    ),
  /** 功能：员工个体知识库（私有桶——base 归属 + 文档计数）。 */
  employeeKnowledge: (employeeId: string) =>
    api.get<{ employee_id: string; bases: EmployeeKnowledgeBase[] }>(
      `/company/employees/${encodeURIComponent(employeeId)}/knowledge`,
    ),
};
