/**
 * 功能：团队记忆待确认数据面（Phase2-2 审批卡片）。
 * 确认权仅用户本人——confirm/reject 走后端鉴权；report 仅会签意见
 * 无落库效力（拍板：上级可选上报环节）。超时项由后端 list 顺带清理（拍板：超时丢弃）。
 */
import { api } from "@/services/api";

/** 待确认记录（GET /api/memory/team-pending）。 */
export interface TeamMemoryPending {
  id: string;
  summary: string;
  employee_name?: string;
  team_name?: string;
  project_name?: string;
  payload: {
    summary?: string;
    project_id?: string;
    team_id?: string;
    employee_id?: string;
    session_id?: string;
    turn_index?: number;
    escalation?: { opinion?: string; reporter_role?: string } | Record<string, never>;
    reason?: string;
    analysis?: string;
    output_preview?: string;
    files?: string[];
    suggested_employee?: string;
    suggested_skill_slug?: string;
    skill_target?: string;
    verdict_action?: string;
  };
  created_at: number;
}

export const teamMemoryApi = {
  /** 功能：待确认列表（后端顺带丢弃超 TTL 项）。 */
  list: () => api.get<{ pending: TeamMemoryPending[] }>("/memory/team-pending"),
  /** 功能：用户本人确认——落 episodic 团队分区；可同步生成技能卡。 */
  confirm: (
    id: string,
    options?: { create_skill?: boolean; skill_name?: string },
  ) =>
    api.post<{ confirmed: boolean; id: string; skill_path?: string }>(
      `/memory/team-pending/${id}/confirm`,
      options ?? {},
    ),
  /** 功能：用户本人驳回（丢弃，不落库）。 */
  reject: (id: string) =>
    api.post<{ rejected: boolean; id: string }>(`/memory/team-pending/${id}/reject`),
  /** 功能：上报上级角色——意见仅存档为建议。 */
  report: (id: string, opinion: string, reporterRole: string) =>
    api.post<{ reported: boolean; id: string }>(`/memory/team-pending/${id}/report`, {
      opinion,
      reporter_role: reporterRole,
    }),
};
