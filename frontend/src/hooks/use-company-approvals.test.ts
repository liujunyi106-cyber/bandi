/**
 * 功能：数据桥纯函数契约——pending 项 → 交互帧（approval_store 来源标记
 * 命名空间前缀、决策端点分流的判据）。
 */
import { describe, expect, it } from "vitest";

import {
  approvalFrameId,
  APPROVAL_FRAME_PREFIX,
  buildApprovalFrame,
  type ApprovalItem,
} from "@/hooks/use-company-approvals";

const ITEM: ApprovalItem = {
  session_id: "role-session::proj::team::emp",
  tool_name: "bash",
  reason: "执行命令需要审批",
  tool_call_id: "tc-123",
  params_digest: "abc",
};

describe("use-company-approvals（B7 数据桥纯函数）", () => {
  it("帧 id 带 approval: 命名空间前缀（ChatView 据此全局可见）", () => {
    const id = approvalFrameId(ITEM);
    expect(id.startsWith(APPROVAL_FRAME_PREFIX)).toBe(true);
    expect(id).toContain("tc-123");
  });

  it("帧携带 approval_store 来源标记与 permission 变体（按钮走决策端点）", () => {
    const frame = buildApprovalFrame(ITEM);
    expect(frame.kind).toBe("permission");
    expect(frame.session_id).toBe(ITEM.session_id);
    expect(frame.payload.source).toBe("approval_store");
    expect(frame.payload.ui_variant).toBe("permission");
    expect(String(frame.payload.message)).toContain("审批");
  });

  it("reason 为空时用工具名兜底消息（可读性）", () => {
    const frame = buildApprovalFrame({ ...ITEM, reason: "" });
    expect(String(frame.payload.message)).toContain("bash");
  });
});
