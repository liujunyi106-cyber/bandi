/**
 * 功能：工作项卡片（看板列内展示）+ 原地展开（不遮罩、不开抽屉）。
 *
 * 收起只给「一眼能判断」的信息：阶段徽标、标题、岗位、时间。
 * 展开才给产出预览、元信息与动作按钮——列宽只有 ~300px，常驻太多内容会挤爆。
 * 动作严格按后端相位约束（`actionsOf`），不做「看起来能点但一定 400」的按钮。
 */
import { ChevronDown, GitBranch, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { useT } from "@/i18n";
import { actionsOf, formatTime, phaseLabelKey, phaseTone } from "@/lib/work-item";
import type { WorkItemSummary } from "@/services/company";

/** 功能：卡片上的动作种类（与 pages/work 的分发一一对应）。 */
export type WorkItemCardAction = "retry" | "abort" | "review-approve" | "review-rework";

export interface WorkItemCardProps {
  item: WorkItemSummary;
  /** 岗位 id → 展示名（缺省直接显示 id）。 */
  roleNames?: Record<string, string>;
  expanded: boolean;
  busy?: boolean;
  onToggle: (id: string) => void;
  onAction: (id: string, kind: WorkItemCardAction, feedback: string) => void;
}

/** 功能：短 id（取工作项 id 的最后一段，模板卡形如 `<team>_<task>`）。 */
function shortId(id: string): string {
  const parts = id.split("_");
  return parts.length > 1 ? parts[parts.length - 1] : id;
}

export function WorkItemCard({
  item,
  roleNames,
  expanded,
  busy,
  onToggle,
  onAction,
}: WorkItemCardProps) {
  const t = useT();
  const [feedback, setFeedback] = useState("");

  const labelKey = phaseLabelKey(item.phase);
  const phaseLabel = labelKey ? t(labelKey) : item.phase;
  const updated = formatTime(item.updated_at ?? item.created_at);
  const actions = actionsOf(item);
  const roleLabel = roleNames?.[item.assignee_role] ?? item.assignee_role ?? t("work.unassigned");

  return (
    <div
      data-card-id={item.id}
      className={`border-chat-hairline bg-card w-full rounded-lg border text-left transition ${
        expanded ? "border-chat-muted/45 shadow-[0_2px_10px_-3px_rgba(17,18,20,0.14)]" : ""
      }`}
    >
      <button
        type="button"
        data-testid={`work-item-${item.id}`}
        onClick={() => onToggle(item.id)}
        aria-expanded={expanded}
        className="hover:bg-chat-muted/[0.03] w-full rounded-lg p-2 text-left"
      >
        <div className="flex flex-wrap items-center gap-1">
          <span className={`rounded px-1.5 py-0.5 text-[9.5px] leading-4 ${phaseTone(item.phase)}`}>
            {phaseLabel}
          </span>
          {item.has_gate ? (
            <span className="flex items-center gap-0.5 rounded border border-[#f3dbd0] bg-[#fdf5f2] px-1.5 py-0.5 text-[9.5px] leading-4 text-[#9a3b1c]">
              <ShieldCheck className="size-2.5" aria-hidden="true" />
              {t("work.badge.gate")}
            </span>
          ) : null}
          {item.dependency_count ? (
            <span className="border-chat-hairline text-chat-muted flex items-center gap-0.5 rounded border px-1.5 py-0.5 text-[9.5px] leading-4">
              <GitBranch className="size-2.5" aria-hidden="true" />
              {item.dependency_count}
            </span>
          ) : null}
          <ChevronDown
            className={`text-chat-muted/60 ml-auto size-3 shrink-0 transition-transform ${
              expanded ? "rotate-180" : ""
            }`}
            aria-hidden="true"
          />
        </div>

        <h3 className="text-chat-ink mt-1.5 line-clamp-2 text-[11.5px] leading-[1.45] font-medium">
          {item.title || item.id}
        </h3>

        <div className="text-chat-muted/80 mt-1.5 flex items-center gap-1.5 text-[10px]">
          <span className="truncate">{roleLabel}</span>
          <span className="ml-auto shrink-0 tabular-nums">{updated}</span>
        </div>
      </button>

      {expanded ? (
        <div className="border-chat-hairline border-t px-2 pt-2 pb-2.5">
          {item.last_output_preview ? (
            <p className="border-chat-hairline text-chat-muted mb-2 line-clamp-4 rounded border bg-[#fafbfc] px-1.5 py-1 text-[10px] leading-[1.55]">
              {item.last_output_preview}
            </p>
          ) : null}

          <div className="text-chat-muted/70 mb-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[9.5px]">
            <span className="font-mono">#{shortId(item.id)}</span>
            <span>{t("work.field.phase")}: {phaseLabel}</span>
          </div>

          {actions.canReview ? (
            <div className="mb-2 flex flex-col gap-1.5">
              <textarea
                value={feedback}
                onChange={(event) => setFeedback(event.target.value)}
                placeholder={t("work.review.feedbackPlaceholder")}
                aria-label={t("work.review.feedbackPlaceholder")}
                data-testid={`work-review-input-${item.id}`}
                rows={2}
                className="border-chat-hairline text-chat-ink placeholder:text-chat-muted/60 min-h-14 w-full resize-y rounded border bg-transparent px-2 py-1.5 text-[10.5px] leading-relaxed outline-none focus:border-chat-muted/45"
              />
              <div className="flex flex-wrap items-center gap-1.5">
                <button
                  type="button"
                  disabled={busy}
                  data-testid={`work-review-pass-${item.id}`}
                  onClick={() => onAction(item.id, "review-approve", feedback.trim())}
                  className="bg-primary text-primary-foreground rounded px-2 py-1 text-[10.5px] font-medium disabled:opacity-50"
                >
                  {t("work.action.pass")}
                </button>
                <button
                  type="button"
                  disabled={busy || !feedback.trim()}
                  data-testid={`work-review-rework-${item.id}`}
                  onClick={() => onAction(item.id, "review-rework", feedback.trim())}
                  className="border-chat-hairline text-chat-muted rounded border px-2 py-1 text-[10.5px] disabled:opacity-50"
                >
                  {t("work.action.reworkSubmit")}
                </button>
              </div>
            </div>
          ) : null}

          <div className="flex flex-wrap items-center gap-1.5">
            {actions.canRetry ? (
              <button
                type="button"
                disabled={busy}
                data-testid={`work-retry-${item.id}`}
                onClick={() => onAction(item.id, "retry", "")}
                className="border-chat-hairline text-chat-muted rounded border px-2 py-1 text-[10.5px] disabled:opacity-50"
              >
                {t("work.action.retry")}
              </button>
            ) : null}
            {actions.canAbort ? (
              <button
                type="button"
                disabled={busy}
                data-testid={`work-abort-${item.id}`}
                onClick={() => onAction(item.id, "abort", "")}
                className="rounded border border-[#f0d2cc] px-2 py-1 text-[10.5px] text-[#9a3b1c] disabled:opacity-50"
              >
                {t("work.action.abort")}
              </button>
            ) : null}
            {!actions.canRetry && !actions.canAbort ? (
              <span className="text-chat-muted/60 text-[10px]">{t("work.action.none")}</span>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
