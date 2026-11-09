/**
 * 功能：设置域 API。
 */
import { api, apiFetch } from "@/services/api";

/** 候选模型（supports_reasoning 驱动思考模式控件显示）。 */
export interface ModelOption {
  name: string;
  provider: string;
  supports_reasoning: boolean;
  reasoning_efforts: string[];
  /** -C：能否关闭思考（false = 强制思考，UI 不渲染「关闭」档）。 */
  supports_thinking_off?: boolean;
}

export interface ModelSettings {
  current_model: string;
  has_api_key: boolean;
  available_models: ModelOption[];
  /** 激活档案摘要。 */
  active_profile?: ProfileSummary | null;
  /** 档案解析后的实际生效模型（无档案时与 current_model 一致）。 */
  effective_model?: string;
  effective_has_api_key?: boolean;
}

/** Provider 档案。 */
export interface LlmProfile {
  profile_id: string;
  name: string;
  provider: string;
  model: string;
  api_key: string;
  base_url: string;
  extra: Record<string, string | number>;
  has_api_key?: boolean;
}

/** 档案摘要（key 一律掩码；API 出参绝不回明文）。 */
export interface ProfileSummary {
  profile_id: string;
  name: string;
  provider: string;
  model: string;
  base_url: string;
  api_key_masked: string;
}

export interface ProfilesPayload {
  active_profile_id: string;
  profiles: (LlmProfile & { has_api_key?: boolean })[];
}

/** 用量分桶。后端 `/usage/stats` 的 total / by_model / by_role。 */
export interface UsageBucket {
  /** 请求数 */
  requests: number;
  /** 归一后的总输入 tokens（Anthropic 的 input 不含缓存部分，后端已归一） */
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  /** 0–1；分母是归一总输入，后端已钳制（不会 >1） */
  cache_hit_rate: number;
}

/** 用量与缓存聚合（；数据源 = 本地请求台账 cost.db）。 */
export interface UsageStats {
  range: string;
  days: number;
  generated_at: number;
  total: UsageBucket;
  by_model: Record<string, UsageBucket>;
  by_project: Record<string, UsageBucket>;
  by_agent?: Record<string, UsageBucket>;
}

export type UsageRange = "7d" | "30d" | "all";

/** 空态（台账不存在 / 首次打开设置）——前端不得因后端空对象而崩。 */
export const EMPTY_USAGE_BUCKET: UsageBucket = {
  requests: 0,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_creation_tokens: 0,
  cache_hit_rate: 0,
};

export const EMPTY_USAGE_STATS: UsageStats = {
  range: "all",
  days: 0,
  generated_at: 0,
  total: EMPTY_USAGE_BUCKET,
  by_model: {},
  by_project: {},
};

export const settingsApi = {
  /** 功能：当前模型 + 候选列表（控制条数据源）。 */
  model: () => api.get<ModelSettings>("/settings/model"),
  /** 功能：切换全局默认模型（控制条切换；下次回合生效）。 */
  setModel: (model: string) => api.put<{ current_model: string }>("/settings/model", { model }),

  // ── Provider 档案─────────────────────────────────────────
  profiles: () => api.get<ProfilesPayload>("/settings/llm/profiles"),
  createProfile: (profile: Partial<LlmProfile> & { profile_id: string; model: string }) =>
    api.post<{ profile: LlmProfile; active_profile_id: string }>("/settings/llm/profiles", profile),
  updateProfile: (profileId: string, profile: Partial<LlmProfile>) =>
    api.put<{ profile: LlmProfile; active_profile_id: string }>(
      `/settings/llm/profiles/${encodeURIComponent(profileId)}`,
      profile,
    ),
  deleteProfile: (profileId: string) =>
    api.del<{ deleted: string; active_profile_id: string }>(
      `/settings/llm/profiles/${encodeURIComponent(profileId)}`,
    ),
  activateProfile: (profileId: string) =>
    api.post<{ active_profile_id: string; profile: LlmProfile | null }>(
      `/settings/llm/profiles/${encodeURIComponent(profileId)}/activate`,
      {},
    ),

  // ── 用量与缓存────────────────────────────────────────────────────
  /** 功能：用量与缓存聚合（设置 → 用量 / 缓存 两个页签共用一次请求）。 */
  usageStats: (range: UsageRange = "all") => api.get<UsageStats>(`/usage/stats?range=${range}`),

  // ── 系统信息 / 存储 / 偏好 / 保留 / MCP ────────────────────────────────
  systemInfo: () => api.get<SystemInfo>("/system/info"),
  storage: () => api.get<StorageInfo>("/system/storage"),
  preferences: () => api.get<PreferenceState>("/settings/preferences"),
  permissionRules: () => api.get<{ rules: PermissionRule[] }>("/settings/permission-rules"),
  savePermissionRules: (rules: Array<{ pattern: string; action: PermissionRuleAction }>) =>
    api.put<{ rules: PermissionRule[] }>("/settings/permission-rules", { rules }),
  approvalGrants: () => api.get<{ grants: ApprovalGrant[] }>("/settings/approval-grants"),
  revokeApprovalGrant: (body: {
    session_key: string;
    tool_name: string;
    params_digest: string;
    scope: string;
  }) => apiFetch<{ revoked: boolean }>("/settings/approval-grants", { method: "DELETE", json: body }),
  savePreferences: (body: {
    permission_preset?: PermissionPreset;
    retention_days?: number;
    web_search?: { tavily_api_key?: string; brave_api_key?: string };
  }) => api.put<PreferenceState>("/settings/preferences", body),
  retentionPreview: (days: number) =>
    api.get<{ days: number; sessions: number }>(`/settings/retention/preview?days=${days}`),
  retentionCleanup: (days: number) =>
    api.post<{ days: number; deleted: number }>("/settings/retention/cleanup", {
      days,
      confirm: true,
    }),
  mcpServers: () => api.get<McpServersPayload>("/mcp/servers"),
  saveMcpGlobal: (body: { autostart?: boolean; lazy_exposure?: boolean; max_exposed_tools?: number }) =>
    api.put<{ ok: boolean }>("/mcp/global", body),
  // 后端 /tools/meta 直接返回数组（不是 {tools:[...]}）——按真实结构声明，避免取错字段拿到空表
  toolsMeta: () => api.get<ToolMeta[]>("/tools/meta"),
  skillsList: () => api.get<{ skills: SkillMeta[] }>("/skills"),
  patchMcpServer: (
    name: string,
    body: Partial<Pick<McpServer, "enabled" | "allowed_tools" | "category" | "homepage">>,
  ) => api.patch<{ ok: boolean }>(`/mcp/servers/${encodeURIComponent(name)}`, body),
  testMcpServer: (name: string) =>
    api.post<{ ok: boolean; tool_count: number; error: string; tools?: string[] }>(
      `/mcp/servers/${encodeURIComponent(name)}/test`,
      {},
    ),
  mcpConfig: () => api.get<McpConfigPayload>("/mcp/config"),
  saveMcpConfig: (content: McpConfigDocument) =>
    api.put<{ ok: boolean; path: string; count: number }>("/mcp/config", content),
  computerStatus: () => api.get<ComputerStatus>("/computer/status"),
  installComputerRuntime: (interpreter = "") =>
    api.post<{ started: boolean; already_running: boolean; state: ComputerInstallState }>(
      "/computer/runtime/install",
      { interpreter },
    ),
  setComputerInterpreter: (path: string) =>
    api.put<{ ok: boolean; interpreter: ComputerStatus["interpreter"] }>("/computer/interpreter", {
      path,
    }),
  setComputerEnabled: (enabled: boolean, confirm = false) =>
    api.put<{ ok: boolean; enabled: boolean; status: ComputerStatus }>("/computer/enabled", {
      enabled,
      confirm,
    }),
};

/** 系统运行信息（设置 → 关于）。 */
export interface SystemInfo {
  version: string;
  python: string;
  uvicorn: string;
  platform: string;
  data_dir: string;
  uptime_s: number;
}

/** 单个数据库文件占用。 */
export interface StorageDb {
  name: string;
  bytes: number;
  mtime: string;
}

/** 数据目录与磁盘占用。 */
export interface StorageInfo {
  data_dir: string;
  dbs: StorageDb[];
  total_bytes: number;
  disk: { total: number; used: number; free: number };
}

/** 权限策略现状（询问范围 + deny 底线）。 */
export interface PreferenceState {
  permission: {
    ask_risk_levels: string[];
    ask_tools: string[];
    bypass_tools: string[];
    deny_tools: string[];
    deny_commands: string[];
  };
  retention_days: number;
  web_search: { tavily_configured: boolean; brave_configured: boolean };
}

/** 权限档位标识（宽松 / 标准 / 严格）。 */
export type PermissionPreset = "loose" | "standard" | "strict";

/** 功能：权限规则（allow/ask/deny） */
export type PermissionRuleAction = "allow" | "ask" | "deny";

export interface PermissionRule {
  pattern: string;
  action: PermissionRuleAction;
  kind: string;
  tool: string;
  content: string;
  source: string;
}

/** 功能：always-allow 授权条目 */
export interface ApprovalGrant {
  session_key: string;
  tool_name: string;
  params_digest: string;
  scope: "session" | "once";
  granted_at: number;
  expires_at: number;
  remaining_s: number;
}

/** MCP 服务器条目（配置 + 运行态推断）。 */
export interface McpServer {
  name: string;
  pool: string;
  transport: string;
  command: string;
  args: string[];
  url: string;
  prefix: string;
  enabled: boolean;
  category: string;
  homepage: string;
  allowed_tools: string[] | null;
  status: "connected" | "idle" | "disabled";
  tool_count: number;
  tools: string[];
}

export interface McpServersPayload {
  servers: McpServer[];
  global: { autostart: boolean; lazy_exposure: boolean; max_exposed_tools: number };
}

/** mcp.json 编辑文档（服务名 → 配置）。 */
export interface McpConfigDocument {
  mcpServers: Record<string, Record<string, unknown>>;
}

export interface McpConfigPayload {
  path: string;
  content: McpConfigDocument;
}

/** 单个工具的目录元数据（`GET /api/tools/meta` 的数组元素）。 */
export interface ToolMeta {
  name: string;
  description?: string;
  category?: string;
  feature_group?: string | null;
  capability_tags?: string[];
  risk_level?: string;
  mcp?: boolean;
}

/** 技能条目（`GET /api/skills` 的 skills 元素）。 */
export interface SkillMeta {
  name: string;
  description?: string;
  tags?: string[];
  team_id?: string;
  always?: boolean;
}

/** 安装状态（后端安装状态机）。 */
export interface ComputerInstallState {
  phase: "idle" | "preparing" | "creating_env" | "installing" | "verifying" | "ready" | "failed" | string;
  progress: number;
  message?: string;
  error?: string;
  log?: string[];
  started_at?: string;
  finished_at?: string;
  updated_at?: string;
}

/** 桌面控制状态（设置 → Computer Use）。 */
export interface ComputerStatus {
  enabled: boolean;
  venv_dir: string;
  venv_exists: boolean;
  python: string;
  python_version: string;
  deps_installed: boolean;
  deps: Record<string, boolean>;
  missing_packages: string[];
  probe_error: string;
  screen: { width?: number; height?: number };
  interpreter: {
    path: string;
    version: string;
    source?: string;
    candidates?: { path: string; version: string }[];
  };
  install: ComputerInstallState;
  safe_mode: boolean;
  /** 坐标系口径：physical = 截图与点击都用物理像素 */
  coordinate_space?: string;
  /** 系统缩放比（如 1.25 表示 125%）；逻辑坐标 × scale = 物理像素 */
  scale?: number;
  platform: string;
  helper: string;
}
