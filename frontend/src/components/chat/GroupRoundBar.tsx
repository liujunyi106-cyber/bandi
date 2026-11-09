import { Check, LoaderCircle, Pause, Play } from "lucide-react";
import { toast } from "sonner";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { companyApi, type GroupRoundSnapshot } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";

const STATUS_KEY: Record<string, string> = {
  speaking: "speaking",
  waiting: "waiting",
  posted: "posted",
  passed: "passed",
  missed: "missed",
  working: "working",
  waiting_approval: "waitingApproval",
  failed: "failed",
  paused: "paused",
  left: "left",
  cancelled: "cancelled",
};

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function GroupRoundBar({ round }: { round?: GroupRoundSnapshot | null }) {
  const t = useT();
  if (!round || round.slots.length === 0) return null;
  const paused = round.status === "paused";
  const completed = round.status === "completed" || round.status === "cancelled";

  const resume = async () => {
    try {
      const payload = await companyApi.resumeRound(round.round_id);
      if (payload.round) useChatStore.getState().applyRound(payload.round);
    } catch (error) {
      toast.error(errorText(error));
    }
  };

  return (
    <div
      className="border-border bg-muted/35 text-muted-foreground flex min-h-8 items-center gap-3 border-b px-4 py-1.5 text-xs"
      data-testid="group-round-bar"
      aria-live="polite"
    >
      <span className="text-foreground inline-flex shrink-0 items-center gap-1.5 font-medium">
        {paused ? (
          <Pause className="h-3.5 w-3.5" aria-hidden />
        ) : completed ? (
          <Check className="h-3.5 w-3.5" aria-hidden />
        ) : (
          <LoaderCircle className="h-3.5 w-3.5 animate-spin" aria-hidden />
        )}
        {t("chat.round.title")}
      </span>
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden">
        {round.slots.map((slot) => (
          <span
            key={slot.slot_id}
            className={cn(
              "truncate",
              slot.status === "speaking" && "text-foreground font-medium",
              slot.status === "failed" && "text-destructive",
            )}
            data-testid={`group-round-slot-${slot.role_id}`}
          >
            {slot.role_name || slot.role_id} ·{" "}
            {t(`chat.round.status.${STATUS_KEY[slot.status] ?? "waiting"}`)}
          </span>
        ))}
      </div>
      {paused ? (
        <button
          type="button"
          className="text-foreground hover:bg-accent inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-0.5 font-medium outline-none disabled:opacity-50"
          onClick={() => void resume()}
          data-testid="group-round-resume"
        >
          <Play className="h-3 w-3" aria-hidden />
          {t("chat.round.resume")}
        </button>
      ) : null}
    </div>
  );
}
