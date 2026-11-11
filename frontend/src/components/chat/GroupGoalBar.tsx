import { Target } from "lucide-react";

import { cn } from "@/lib/utils";
import type { GroupGoalSnapshot } from "@/services/company";

const STATUS: Record<string, { label: string; tone: string }> = {
  drafting: { label: "对齐中", tone: "text-muted-foreground" },
  active: { label: "进行中", tone: "text-success" },
  paused: { label: "已暂停", tone: "text-warning" },
  completed: { label: "已完成", tone: "text-success" },
  stopped: { label: "已停止", tone: "text-muted-foreground" },
};

export function GroupGoalBar({ goal }: { goal?: GroupGoalSnapshot | null }) {
  if (!goal) return null;
  const status = STATUS[goal.status] ?? {
    label: goal.status,
    tone: "text-muted-foreground",
  };
  const hasStages = goal.total > 0;
  const current = Math.max(0, Math.min(goal.current, goal.total));
  return (
    <div
      className="border-chat-hairline bg-chat-bg flex min-h-[46px] items-center gap-2 border-b px-3 py-[6px]"
      data-testid="group-goal-bar"
      aria-live="polite"
    >
      <Target className="text-foreground size-4 shrink-0" strokeWidth={1.7} aria-hidden />
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <span className="text-foreground truncate text-[12.5px] font-semibold tracking-[-0.015em]">
          {goal.title || "正在对齐群聊目标"}
        </span>
        <span
          className={cn(
            "border-border bg-muted/30 inline-flex h-5 shrink-0 items-center gap-1.5 rounded-full border px-2 text-[10px] font-medium",
            status.tone,
          )}
          data-testid="group-goal-status"
        >
          <span className="size-[5px] rounded-full bg-current" aria-hidden />
          {status.label}
        </span>
      </div>
      {hasStages ? (
        <>
          <div className="flex shrink-0 items-center gap-2" data-testid="group-goal-progress">
            <div className="flex gap-[3px]" aria-hidden>
              {Array.from({ length: goal.total }, (_, index) => (
                <span
                  key={index}
                  className={cn(
                    "h-[3px] w-[19px] rounded-full",
                    index + 1 < current
                      ? "bg-primary"
                      : index + 1 === current
                        ? "bg-muted-foreground/60"
                        : "bg-border",
                  )}
                />
              ))}
            </div>
            <span className="text-muted-foreground text-[10.5px] tabular-nums">
              {current} / {goal.total}
            </span>
          </div>
          <span className="text-muted-foreground hidden max-w-[250px] shrink-0 truncate text-[10.5px] lg:inline">
            {goal.stage_title
              ? current > 0
                ? `阶段 ${current}/${goal.total} · ${goal.stage_title}`
                : goal.stage_title
              : ""}
          </span>
        </>
      ) : null}
    </div>
  );
}
