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
  /** 冲突裁决的逐字对照（旧值=已落库条目，新值=本次候选）。 */
  diff?: { old?: string; new?: string };
  /** 候选全文（脱敏后，最长 4000 字）——审批人据此看完整内容。 */
  content?: string;
  structured?: unknown;
  draft?: MemoryDraftMeta;
}

/** 审批队列响应：pending 为已晋级到审批的条目，drafts 为仍在草稿池的条数。 */
export interface MemoryPendingPayload {
  pending: MemoryPending[];
  /** 草稿池条数（未超容量/未到时间兜底前不进审批队列）。 */
  drafts?: number;
}

export const memoryApprovalApi = {
  list: () => api.get<MemoryPendingPayload>("/memory/pending"),
  approve: (id: string) =>
    api.post<{ approved: boolean; id: string; target_id?: string }>(
      `/memory/pending/${encodeURIComponent(id)}/approve`,
    ),
  reject: (id: string) =>
    api.post<{ rejected: boolean }>(`/memory/pending/${encodeURIComponent(id)}/reject`),
};
