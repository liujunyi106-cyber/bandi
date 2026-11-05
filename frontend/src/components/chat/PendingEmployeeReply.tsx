/**
 * 功能：员工回复占位行——发送/开跑即显示头像与名字，避免等 LLM 结束才出现身份。
 */
import { LoaderCircle } from "lucide-react";

import { EmployeeIdentityHeader } from "@/components/chat/EmployeeIdentityHeader";

interface PendingEmployeeReplyProps {
  roleId: string;
  name: string;
  metadata?: Record<string, unknown>;
}

export function PendingEmployeeReply({
  roleId,
  name,
  metadata,
}: PendingEmployeeReplyProps) {
  return (
    <div
      className="flex w-full flex-col items-start"
      data-testid="pending-employee-reply"
      data-role-id={roleId}
      aria-live="polite"
    >
      <EmployeeIdentityHeader name={name} metadata={metadata} />
      <span
        className="text-chat-muted flex items-center gap-1 text-xs"
        data-testid="pending-employee-reply-status"
      >
        <LoaderCircle className="size-3 animate-spin" aria-hidden />
        正在回复中…
      </span>
    </div>
  );
}
