/**
 * 功能：——员工 headless 审批（ApprovalStore pending）轮询桥：
 * 拉 GET /api/company/approvals → 构造 permission 帧写入 chat-store.interactions
 * → 现有 InteractionCard 渲染（approval_store 来源经 approval-decision 端点决策）
 * → 决策后 pending 消失、下一轮同步移除帧。失败静默（不打扰主链路）。
 */
import { useEffect } from "react";

import { sessionsApi } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";

const POLL_INTERVAL_MS = 5000;
/** 审批帧命名空间前缀（ChatView 据此放宽可见性——全局可见可决策）。 */
export const APPROVAL_FRAME_PREFIX = "approval:";

export interface ApprovalItem {
  session_id: string;
  tool_name: string;
  reason: string;
  tool_call_id: string;
  params_digest: string;
  /** 工具意图说明（结构化写入工具的「调用什么工具、提取什么内容」）。 */
  detail?: string;
  /** 权限卡中文提问（结构化写入工具写明目标库）。 */
  prompt?: string;
  /** 命中的权限规则与来源（审批卡解释）。 */
  rule_pattern?: string;
  rule_source?: string;
}

export function approvalFrameId(item: ApprovalItem): string {
  return `${APPROVAL_FRAME_PREFIX}${item.session_id}:${item.tool_call_id}`;
}

/** 功能：pending 项 → InteractionFrame（approval_store 来源标记，按钮走决策端点）。 */
export function buildApprovalFrame(item: ApprovalItem): {
  interaction_id: string;
  session_id: string;
  kind: string;
  payload: Record<string, unknown>;
} {
  return {
    interaction_id: approvalFrameId(item),
    session_id: item.session_id,
    kind: "permission",
    payload: {
      ui_variant: "permission",
      source: "approval_store",
      message: item.prompt || item.reason || `工具 ${item.tool_name} 请求审批`,
      tool_name: item.tool_name,
      // 多 pending 并存时决策必须精确到被点的这一条（端点按 tool_call_id 命中）
      tool_call_id: item.tool_call_id,
      detail: item.detail ?? "",
      rule_pattern: item.rule_pattern ?? "",
      rule_source: item.rule_source ?? "",
    },
  };
}

export function useCompanyApprovals(): void {
  useEffect(() => {
    let cancelled = false;
    const sync = async () => {
      try {
        const data = await sessionsApi.companyApprovals();
        if (cancelled) return;
        const store = useChatStore.getState();
        const seen = new Set<string>();
        for (const item of data.approvals) {
          const frame = buildApprovalFrame(item);
          seen.add(frame.interaction_id);
          store.addInteraction(frame);
        }
        // 已决策/消失的 pending → 移除对应帧（仅本命名空间）
        for (const existing of Object.keys(store.interactions)) {
          if (existing.startsWith(APPROVAL_FRAME_PREFIX) && !seen.has(existing)) {
            store.resolveInteraction(existing);
          }
        }
      } catch {
        // 审批桥失败静默——下一轮重试
      }
    };
    void sync();
    const timer = window.setInterval(() => void sync(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);
}
