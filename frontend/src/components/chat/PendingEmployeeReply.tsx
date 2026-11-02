/**
 * 功能：员工回复占位行——发送/开跑即显示头像与名字，避免等 LLM 结束才出现身份。
 */
import { LoaderCircle } from "lucide-react";

import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";

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
      className="text-chat-muted flex items-center gap-1.5 pl-5 text-sm leading-none"
      data-testid="pending-employee-reply"
      data-role-id={roleId}
      aria-live="polite"
    >
      <EmployeeAvatar name={name} metadata={metadata} className="size-6 rounded-full text-xs" />
      <span className="text-foreground font-medium">{name}</span>
      <span className="text-chat-muted flex items-center gap-1 text-xs">
        <LoaderCircle className="size-3 animate-spin" aria-hidden />
        正在回复中…
      </span>
    </div>
  );
}
