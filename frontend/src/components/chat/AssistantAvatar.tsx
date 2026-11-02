import { cn } from "@/lib/utils";

/** 功能：助理品牌头像（统一使用品牌 Logo，等比缩放不裁切不变形）。 */
export function AssistantAvatar({ className }: { className?: string }) {
  return (
    <span
      className={cn("inline-flex shrink-0 items-center justify-center", className)}
      data-testid="assistant-avatar"
    >
      <img
        src={`${import.meta.env.BASE_URL}assistant-avatar.svg`}
        alt=""
        aria-hidden="true"
        draggable={false}
        className="size-full object-contain"
      />
    </span>
  );
}
