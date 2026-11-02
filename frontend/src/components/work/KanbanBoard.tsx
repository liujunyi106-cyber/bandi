/**
 * 功能：工作项看板——四列投影（待办 / 进行中 / 待审 / 已完成）。
 * 列由 phase 归一（`columnOfPhase`，与后端 kanban_column 同口径），不支持拖拽改列：
 * 改列等于改 phase，只能走相位转移表，拖了也会被打回。
 *
 * 看板是执行投影：不做规划、不做批量审批。已完成列默认只铺最近 20 张
 * （真实库里有几十张历史卡），其余「载入更早」。
 */
import { useMemo, useState } from "react";

import { WorkItemCard, type WorkItemCardAction } from "@/components/work/WorkItemCard";
import { useT } from "@/i18n";
import { KANBAN_COLUMNS, groupByColumn, sortByUpdated } from "@/lib/work-item";
import type { WorkItemSummary } from "@/services/company";

/** 功能：已完成列一次渲染的卡片数（命中历史上限时给「载入更早」）。 */
const DONE_PAGE_SIZE = 20;

export interface KanbanBoardProps {
  items: WorkItemSummary[];
  roleNames?: Record<string, string>;
  expandedId: string | null;
  busy?: boolean;
  onToggle: (id: string) => void;
  onAction: (id: string, kind: WorkItemCardAction, feedback: string) => void;
}

export function KanbanBoard({
  items,
  roleNames,
  expandedId,
  busy,
  onToggle,
  onAction,
}: KanbanBoardProps) {
  const t = useT();
  const [doneLimit, setDoneLimit] = useState(DONE_PAGE_SIZE);
  const grouped = useMemo(() => groupByColumn(sortByUpdated(items)), [items]);

  return (
    <div
      data-testid="kanban-board"
      className="grid min-h-0 flex-1 grid-cols-4 gap-3 overflow-hidden pt-4 pb-4"
    >
      {KANBAN_COLUMNS.map((column) => {
        const all = grouped[column.key];
        const isDone = column.key === "done";
        const cards = isDone ? all.slice(0, doneLimit) : all;
        const hiddenDone = isDone ? Math.max(0, all.length - cards.length) : 0;

        return (
          <section
            key={column.key}
            data-testid={`kanban-column-${column.key}`}
            className="border-chat-hairline bg-card flex min-h-0 flex-col overflow-hidden rounded-xl border"
          >
            <div className="flex flex-none items-center gap-1.5 px-2.5 py-2">
              <span
                className="h-2.5 w-[3px] flex-none rounded-sm"
                style={{ background: column.color }}
                aria-hidden="true"
              />
              <b className="text-chat-ink text-[11.5px] font-semibold">{t(column.labelKey)}</b>
              <span
                className="text-chat-muted/70 text-[10px] tabular-nums"
                data-testid={`kanban-count-${column.key}`}
              >
                {all.length}
              </span>
            </div>

            <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto p-2">
              {cards.map((item) => (
                <WorkItemCard
                  key={item.id}
                  item={item}
                  roleNames={roleNames}
                  expanded={expandedId === item.id}
                  busy={busy}
                  onToggle={onToggle}
                  onAction={onAction}
                />
              ))}
              {hiddenDone > 0 ? (
                <button
                  type="button"
                  data-testid="kanban-done-more"
                  onClick={() => setDoneLimit((value) => value + DONE_PAGE_SIZE)}
                  className="text-chat-muted hover:text-chat-ink border-chat-hairline mt-1 rounded border border-dashed py-1.5 text-[10px]"
                >
                  {t("work.loadMore").replace("{count}", String(hiddenDone))}
                </button>
              ) : null}
            </div>
          </section>
        );
      })}
    </div>
  );
}
