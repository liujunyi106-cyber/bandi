/**
 * 功能：员工身份头统一渲染——头像 + 名字，占位/过程块/回复卡片共用同一布局。
 */
import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { cn } from "@/lib/utils";

interface EmployeeIdentityHeaderProps {
  name: string;
  metadata?: Record<string, unknown> | null;
  testId?: string;
  avatarTestId?: string;
  className?: string;
}

export function EmployeeIdentityHeader({
  name,
  metadata,
  testId,
  avatarTestId,
  className,
}: EmployeeIdentityHeaderProps) {
  return (
    <span
      data-testid={testId}
      className={cn(
        "text-chat-muted mb-1 flex items-center gap-1.5 text-sm leading-none",
        className,
      )}
    >
      <EmployeeAvatar
        name={name}
        metadata={metadata}
        className="size-6 rounded-full text-xs"
        testId={avatarTestId}
      />
      <span className="text-foreground font-medium">{name}</span>
    </span>
  );
}
