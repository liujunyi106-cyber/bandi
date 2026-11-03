/**
  * 功能：图谱三态占位（loading / error / empty，）。
 * 空态给「为什么还没有」的解释与去向提示，不用光秃秃的空列表。
 */
import { Loader2, Network, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

interface GraphStateProps {
  tone: "loading" | "error" | "empty";
  /** error 态的技术细节（可折叠展示，便于排查）。 */
  detail?: string;
  onRetry?: () => void;
  /** 覆盖空态标题（如「没有匹配的实体」——库非空但过滤无结果，语义不同于库为空）。 */
  title?: string;
  /** 覆盖空态提示文案。 */
  message?: string;
}

export function GraphState({ tone, detail, onRetry, title, message }: GraphStateProps) {
  const t = useT();

  if (tone === "loading") {
    return (
      <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-3">
        <Loader2 className="size-5 animate-spin" aria-hidden="true" />
        <p className="text-sm">{t("graph.loading")}</p>
      </div>
    );
  }

  const isError = tone === "error";

  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
      <div
        className={
          isError
            ? "bg-destructive/10 text-destructive flex size-10 items-center justify-center rounded-full"
            : "bg-muted text-muted-foreground flex size-10 items-center justify-center rounded-full"
        }
      >
        {isError ? (
          <TriangleAlert className="size-5" aria-hidden="true" />
        ) : (
          <Network className="size-5" aria-hidden="true" />
        )}
      </div>

      <h3 className="text-foreground text-sm font-semibold">
        {isError ? t("graph.errorTitle") : (title ?? t("graph.emptyTitle"))}
      </h3>
      <p className="text-muted-foreground max-w-sm text-xs leading-relaxed">
        {isError ? (detail ?? t("graph.errorTitle")) : (message ?? t("graph.emptyHint"))}
      </p>

      {isError && onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("graph.retry")}
        </Button>
      ) : null}
    </div>
  );
}
