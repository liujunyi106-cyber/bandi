/**
 * 功能：团队域数据面（员工画廊 / 员工档案 / 团队管理）。
 * 聚合现有公司域端点：员工、角色、团队、工作项、技能、工具、模型；员工状态由工作项投影推导。
 */
import { ApiError, api } from "@/services/api";
import {
  companyApi,
  type EmployeeSummary,
  type OrgTeamSummary,
  type TeamTemplateSummary,
  type WorkItemSummary,
} from "@/services/company";
import { configuredModelOptions } from "@/lib/model-options";
import { settingsApi, type ModelSettings } from "@/services/settings";

/** 模板实例化报告（POST /company/teams/{id}/instantiate）。 */
export interface InstantiateReport {
  team_id: string;
  prefix: string;
  role_ids: string[];
  created_role_ids: string[];
  skipped_role_ids: string[];
  employee_ids: string[];
  created_employee_ids: string[];
  skipped_employee_ids: string[];
  seat_role_ids: string[];
  profile_ids: string[];
  warnings: string[];
}

/** 组织角色（GET /api/company/roles）。 */
export interface OrgRoleSummary {
  id: string;
  name: string;
  reports_to: string | null;
  responsibility: string;
  is_final_decider: boolean;
  tool_tags: string[];
  team_id?: string;
  can_spawn?: string[];
}

/** 员工资源绑定（AgentProfile.resources）。 */
export interface EmployeeResource {
  resource_type: string;
  resource_id: string;
  status: string;
  metadata?: Record<string, unknown>;
}

/** 员工档案（GET /api/company/employees/{id}/profile）。 */
export interface EmployeeProfile {
  profile_id: string;
  employee_id: string;
  name: string;
  description: string;
  persona_prompt: string;
  status: string;
  resources: EmployeeResource[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** 员工私有知识库（GET /api/company/employees/{id}/knowledge）。 */
export interface EmployeeKnowledgeBase {
  base_id: string;
  name: string;
  project_id: string;
  document_count: number;
}

/** 已安装技能（GET /api/skills）。 */
export interface SkillSummary {
  name: string;
  description: string;
  tags: string[];
  team_id: string;
  always: boolean;
}

/** 工具目录条目（GET /api/tools/meta）。 */
export interface ToolMeta {
  name: string;
  description?: string;
  capability_tags?: string[];
  feature_group?: string;
}

/** 知识库清单项（GET /api/company/knowledge/bases）。 */
export interface KnowledgeBaseOption {
  base_id: string;
  name: string;
  project_id: string;
  employee_id: string;
  document_count: number;
}

/** SOP 卡片（GET /api/company/skills/cards）。 */
export interface SopCardOption {
  skill_id: string;
  name: string;
  description: string;
  version: string;
  business_domain: string;
}

/** 团队页聚合数据。 */
export interface TeamOverview {
  employees: EmployeeSummary[];
  roles: OrgRoleSummary[];
  orgTeams: OrgTeamSummary[];
  templates: TeamTemplateSummary[];
  workItems: WorkItemSummary[];
  skills: SkillSummary[];
  tools: ToolMeta[];
  models: ModelSettings | null;
}

/** 人才模板（GET /api/company/talent/templates）。 */
export interface TalentTemplateSummary {
  name: string;
  category: string;
  description?: string;
  role_id?: string;
  domains?: string[];
}

/** 员工能力统计（技能 / 知识库 / SOP）。 */
export interface EmployeeStats {
  skills: number;
  knowledge: number;
  sop: number;
}

export const EMPTY_EMPLOYEE_STATS: EmployeeStats = { skills: 0, knowledge: 0, sop: 0 };

/** 功能：从档案资源绑定推导卡片能力统计（技能回退到员工技能清单）。 */
export function statsFromProfile(
  profile: EmployeeProfile | null,
  skillRefs: string[] = [],
): EmployeeStats {
  if (!profile) return { skills: skillRefs.length, knowledge: 0, sop: 0 };
  const active = profile.resources.filter((item) => item.status === "active");
  return {
    skills: Math.max(
      skillRefs.length,
      active.filter((item) => item.resource_type === "general_skill").length,
    ),
    knowledge: active.filter((item) => item.resource_type === "knowledge_base").length,
    sop: active.filter((item) => item.resource_type === "skill").length,
  };
}

/** 功能：员工关联的未完成工作项（按角色过滤，排除已完成列）。 */
export function activeItemsOf(
  employee: EmployeeSummary,
  items: WorkItemSummary[],
): WorkItemSummary[] {
  return items.filter((item) => {
    if (item.assignee_role !== employee.role_id) return false;
    return (item.column ?? "") !== "done";
  });
}

/** 功能：解析员工所属团队名（role_id → org team）。 */
export function teamNameOf(employee: EmployeeSummary, teams: OrgTeamSummary[]): string {
  for (const team of teams) {
    if (team.roles.some((role) => role.role_id === employee.role_id)) return team.name;
  }
  return "";
}

/** 功能：解析员工汇报对象名（role.reports_to → role.name）。 */
export function reportsToName(employee: EmployeeSummary, roles: OrgRoleSummary[]): string {
  const role = roles.find((item) => item.id === employee.role_id);
  if (!role || !role.reports_to) return "";
  return roles.find((item) => item.id === role.reports_to)?.name ?? role.reports_to;
}

/** 功能：把异常压成可读文案。 */
export function describeError(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

/** 功能：安全取列表（单端点失败降级为空，不阻断整页）。 */
async function safeList<T>(task: Promise<T>, fallback: T): Promise<T> {
  try {
    return await task;
  } catch {
    return fallback;
  }
}

export const teamApi = {
  /** 功能：员工画廊聚合加载（员工/角色/团队/模板/工作项/技能/工具/模型）。 */
  async overview(): Promise<TeamOverview> {
    const [employees, roles, orgTeams, templates, workItems, skills, tools, models, profiles]
      = await Promise.all([
        companyApi.employees(true),
        safeList(api.get<{ roles: OrgRoleSummary[] }>("/company/roles"), { roles: [] }),
        safeList(companyApi.orgTeams(), { teams: [] }),
        safeList(companyApi.teams(), { teams: [] }),
        safeList(companyApi.workItems(), { items: [], column_counts: {} }),
        safeList(api.get<{ skills: SkillSummary[] }>("/skills"), { skills: [] }),
        safeList(api.get<ToolMeta[]>("/tools/meta"), []),
        safeList(settingsApi.model(), null),
        safeList(settingsApi.profiles(), { active_profile_id: "", profiles: [] }),
      ]);
    const filteredModels: ModelSettings | null = models
      ? {
          ...models,
          available_models: configuredModelOptions(
            models.available_models ?? [],
            profiles.profiles ?? [],
            models.effective_model ?? models.current_model,
            Boolean(models.effective_has_api_key ?? models.has_api_key),
          ),
        }
      : null;
    return {
      employees: employees.employees,
      roles: roles.roles,
      orgTeams: orgTeams.teams,
      templates: templates.teams,
      workItems: workItems.items,
      skills: skills.skills,
      tools,
      models: filteredModels,
    };
  },
  /** 功能：读取员工档案（含技能与资源绑定）。 */
  profile: (employeeId: string) =>
    api.get<{ profile: EmployeeProfile }>(
      `/company/employees/${encodeURIComponent(employeeId)}/profile`,
    ),
  /** 功能：读取员工私有知识库。 */
  knowledge: (employeeId: string) =>
    api.get<{ employee_id: string; bases: EmployeeKnowledgeBase[] }>(
      `/company/employees/${encodeURIComponent(employeeId)}/knowledge`,
    ),
  /** 功能：待审记忆写入（员工档案记忆 Tab 的待办入口）。 */
  pendingMemories: () => api.get<{ pending: unknown[] }>("/memory/pending"),
  /** 功能：注册员工（可选指定模型 llm_config.default_model）。 */
  createEmployee: (input: {
    employee_id: string;
    name: string;
    role_id: string;
    description?: string;
    llm_config?: { default_model: string; thinking_mode?: string } | null;
    metadata?: Record<string, unknown>;
  }) => api.post<EmployeeSummary>("/company/employees", input),
  /** 功能：更新员工字段（姓名/描述/角色/状态/模型）。 */
  updateEmployee: (
    employeeId: string,
    patch: Partial<{
      name: string;
      role_id: string;
      description: string;
      status: string;
      llm_config: { default_model?: string; thinking_mode?: string } | null;
      metadata: Record<string, unknown>;
    }>,
  ) => api.put<EmployeeSummary>(`/company/employees/${encodeURIComponent(employeeId)}`, patch),
  /** 功能：退役员工（status=retired）。 */
  retireEmployee: (employeeId: string) =>
    api.del<EmployeeSummary>(`/company/employees/${encodeURIComponent(employeeId)}`),
  /** 功能：硬删除员工、角色、档案与绑定。 */
  purgeEmployee: (employeeId: string) =>
    api.post<{ purged: boolean; role_deleted: boolean; tombstone: Record<string, string> }>(
      `/company/employees/${encodeURIComponent(employeeId)}/purge`,
      { confirm: true },
    ),
  /** 功能：实例化团队模板（新建团队：命名 team_id 覆盖模板默认）。 */
  instantiateTeam: (templateId: string, teamId: string, teamName = "") =>
    api.post<{ report: InstantiateReport }>(
      `/company/teams/${encodeURIComponent(templateId)}/instantiate`,
      { team_id: teamId, team_name: teamName },
    ),
  /** 功能：人才模板清单（模板 Tab 人才组数据源）。 */
  talentTemplates: () =>
    api.get<{ templates: TalentTemplateSummary[] }>("/company/talent/templates"),
  /** 功能：按人才模板招募单个员工（可指定团队）。 */
  hireTalent: (input: {
    template_name: string;
    name?: string;
    role_id?: string;
    team_id?: string;
  }) => api.post<{ employee_id: string; name: string }>("/company/talent/hire", input),
  /** 功能：把团队当前组织快照导出为模板 JSON（浏览器下载）。 */
  exportOrgTeam: async (teamId: string): Promise<string> => {
    const response = await fetch(
      `/api/company/org-teams/${encodeURIComponent(teamId)}/export`,
      { headers: { Accept: "application/json" } },
    );
    if (!response.ok) {
      throw new ApiError(response.status, `导出失败（HTTP ${response.status}）`);
    }
    const text = await response.text();
    const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${teamId}.team.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    return `${teamId}.team.json`;
  },
  /** 功能：创建岗位（员工名即岗位名，1:1 绑定）。 */
  createRole: (input: {
    id: string;
    name: string;
    responsibility?: string;
    reports_to?: string | null;
    can_spawn?: string[];
    team_id?: string;
  }) => api.post<OrgRoleSummary>("/company/roles", input),
  /** 功能：更新岗位组织关系（团队 / 汇报 / 可派生 / 名称 / 职责）。 */
  updateRole: (
    roleId: string,
    patch: Partial<{
      name: string;
      responsibility: string;
      reports_to: string | null;
      can_spawn: string[];
      team_id: string;
    }>,
  ) => api.patch<OrgRoleSummary>(`/company/roles/${encodeURIComponent(roleId)}`, patch),
  /** 功能：批量把角色拉入团队（幂等；已属其他团队时转移）。 */
  addRolesToTeam: (teamId: string, roleIds: string[]) =>
    api.post<{ team_id: string; role_ids: string[] }>(
      `/company/teams/${encodeURIComponent(teamId)}/roles`,
      { role_ids: roleIds },
    ),
  /** 功能：创建自定义团队（中文名 + 至少两名员工 + 上下级）。 */
  createTeam: (input: {
    name: string;
    team_id?: string;
    members: Array<{ employee_id: string; reports_to?: string }>;
  }) => api.post<{ team_id: string; name: string }>("/company/teams/custom", input),
  /** 功能：解散团队（清空角色 team_id 并从项目解绑，不删除角色与员工）。 */
  dissolveTeam: (teamId: string) =>
    api.del<{ team_id: string; detached_roles: number }>(
      `/company/org-teams/${encodeURIComponent(teamId)}`,
    ),
  /** 功能：绑定员工能力资源（SOP / 技能 / 知识库 / 工具）。 */
  bindResource: (employeeId: string, resourceType: string, resourceId: string) =>
    api.post<{ bound: boolean }>(`/company/employees/${encodeURIComponent(employeeId)}/resources`, {
      resource_type: resourceType,
      resource_id: resourceId,
    }),
  /** 功能：解绑员工能力资源。 */
  unbindResource: (employeeId: string, resourceType: string, resourceId: string) =>
    api.del<{ unbound: boolean }>(
      `/company/employees/${encodeURIComponent(employeeId)}/resources/${encodeURIComponent(
        resourceType,
      )}/${encodeURIComponent(resourceId)}`,
    ),
  /** 功能：知识库清单（employee_id 为空返回全部）。 */
  knowledgeBases: (employeeId = "") =>
    api.get<{ bases: KnowledgeBaseOption[] }>(
      `/company/knowledge/bases${employeeId ? `?employee_id=${encodeURIComponent(employeeId)}` : ""}`,
    ),
  /** 功能：SOP 卡片清单。 */
  skillCards: () => api.get<{ cards: SopCardOption[] }>("/company/skills/cards"),
  /** 功能：导入本地资源（knowledge 文本 / sop 卡片 / skill SKILL.md）。 */
  importResource: (
    kind: string,
    file: File,
    options: { baseId?: string; employeeId?: string; name?: string } = {},
  ) => {
    const form = new FormData();
    form.append("file", file);
    const params = new URLSearchParams({ kind });
    if (options.baseId) params.set("base_id", options.baseId);
    if (options.employeeId) params.set("employee_id", options.employeeId);
    if (options.name) params.set("name", options.name);
    return api.postForm<{ imported: boolean; kind: string; skill_id?: string; base_id?: string }>(
      `/company/resources/import?${params.toString()}`,
      form,
    );
  },
};
