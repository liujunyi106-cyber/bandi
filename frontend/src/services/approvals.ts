/**
 * 功能：个人记忆的待批准数据面（通知中心内审批）。
 * 用户点批准才落库，批准前均为候选状态。
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

export const memoryApprovalApi = {
  list: () => api.get<{ pending: MemoryPending[] }>("/memory/pending"),
  approve: (id: string) =>
    api.post<{ approved: boolean; id: string }>(`/memory/pending/${encodeURIComponent(id)}/approve`),
  reject: (id: string) =>
    api.post<{ rejected: boolean }>(`/memory/pending/${encodeURIComponent(id)}/reject`),
};
