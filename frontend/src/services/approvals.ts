/**
 * 功能：个人记忆与技能晋升的待批准数据面（通知中心内审批）。
 * 与团队记忆同属 MemoryPendingStore / SkillPendingStore——用户点批准才落库，
 * 批准前均为候选状态。
 */
import { api } from "@/services/api";

/** 草稿元数据（payload.draft）——决定行内标签与写入目标展示。 */
export interface MemoryDraftMeta {
  kind?: string;
  target?: string;
}

/** 待批准记忆（GET /api/memory/pending 条目）。 */
export interface MemoryPending {
  id: string;
  subsystem: string;
  action: string;
  summary: string;
  origin?: string;
  /** 提名来源（"user_like" = 用户点赞提名；空 = 系统自动提名）。 */
  origin_source?: string;
  created_at?: number;
  conflict?: boolean;
  old_value?: string;
  draft?: MemoryDraftMeta;
}

/** 待批准技能（GET /api/skills/pending 条目）。 */
export interface SkillPending {
  id: string;
  name?: string;
  summary?: string;
  description?: string;
  employee_id?: string;
  created_at?: number;
}

export const memoryApprovalApi = {
  list: () => api.get<{ pending: MemoryPending[] }>("/memory/pending"),
  approve: (id: string) =>
    api.post<{ approved: boolean; id: string }>(`/memory/pending/${encodeURIComponent(id)}/approve`),
  reject: (id: string) =>
    api.post<{ rejected: boolean }>(`/memory/pending/${encodeURIComponent(id)}/reject`),
};

export const skillApprovalApi = {
  list: () => api.get<{ pending: SkillPending[] }>("/skills/pending"),
  approve: (id: string) =>
    api.post<{ approved: boolean }>(`/skills/pending/${encodeURIComponent(id)}/approve`),
  reject: (id: string) =>
    api.post<{ rejected: boolean }>(`/skills/pending/${encodeURIComponent(id)}/reject`),
};
