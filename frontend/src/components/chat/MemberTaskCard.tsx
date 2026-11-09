/**
 * 功能：成员任务卡（员工回复派发的子任务卡）——回复消息下方渲染。
 *
 * （2026-10-16）：任务锁定后回复卡内显示「成员任务」块——
 * 标题 icon + 「成员任务」、指派给 X、【任务 #N】标题 + 折叠箭头展开完整任务
 * （description）；串行链每派一环一块、并行并列多块；状态标签随卡推进。
 */
import { ChevronDown, ListTodo } from "lucide-react";
import { useState } from "react";

import { useT } from "@/i18n";
import { resolveEmployeeName } from "@/lib/employee";
import type { EmployeeSummary, WorkItemSummary } from "@/services/company";
import { cn } from "@/lib/utils";

import { useEmployees } from "@/hooks/use-employees";

const STATUS_LABEL: Record<string, string> = {
  pending: "chat.memberTask.pending",
  in_progress: "chat.memberTask.inProgress",
  in_review: "chat.memberTask.inReview",
  done: "chat.memberTask.done",
  failed: "chat.memberTask.failed",
  cancelled: "chat.memberTask.cancelled",
};

function statusLabelKey(item: WorkItemSummary): string {
  const phase = String(item.phase ?? "").toLowerCase();
  if (phase === "failed" || phase === "cancelled") return STATUS_LABEL[phase];
  const column = String(item.column ?? "");
  if (column && STATUS_LABEL[column]) return STATUS_LABEL[column];
  if (["approved", "done", "completed"].includes(phase)) return STATUS_LABEL.done;
  if (["running", "executing", "claimed"].includes(phase)) return STATUS_LABEL.in_progress;
  if (["needs_attention", "awaiting_review", "awaiting_approval"].includes(phase))
    return STATUS_LABEL.inReview;
  return STATUS_LABEL.pending;
}

function MemberTaskRow({
  index,
  item,
  employees,
  defaultOpen,
}: {
  index: number;
  item: WorkItemSummary;
  employees: EmployeeSummary[];
  defaultOpen: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(defaultOpen);
  const assignee = item.assignee_role ? resolveEmployeeName(employees, item.assignee_role) : "";
  return (
    <div
      data-testid={`member-task-${item.id}`}
      className="border-chat-hairline rounded-lg border px-2.5 py-1.5"
    >
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          data-testid={`member-task-toggle-${item.id}`}
          className="text-chat-muted hover:text-chat-ink mt-0.5 shrink-0"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </button>
        <div className="min-w-0 flex-1">
          <p className="text-chat-ink text-xs font-medium break-words">
            {t("chat.memberTask.taskNo")}
            {index + 1}
            {t("chat.memberTask.taskNoSuffix")} {item.title}
          </p>
          {assignee ? (
            <p className="text-chat-muted mt-0.5 text-[11px]">
              {t("chat.memberTask.assignee")} {assignee}
            </p>
          ) : null}
          {open && item.description ? (
            <p className="text-chat-muted mt-1 text-[11px] whitespace-pre-wrap">
              {item.description}
            </p>
          ) : null}
        </div>
        <span
          data-testid={`member-task-status-${item.id}`}
          className="text-chat-muted shrink-0 text-[10px]"
        >
          {t(statusLabelKey(item))}
        </span>
      </div>
    </div>
  );
}

export function MemberTaskCards({
  items,
  className,
}: {
  items: WorkItemSummary[];
  className?: string;
}) {
  const t = useT();
  const { employees } = useEmployees();
  if (items.length === 0) return null;
  return (
    <div
      data-testid="member-task-cards"
      data-count={items.length}
      className={cn("flex w-full flex-col gap-1.5", className)}
    >
      <p className="text-chat-muted flex items-center gap-1 text-xs font-medium">
        <ListTodo className="size-3.5" />
        {items.length > 1 ? t("chat.memberTask.titlePlural") : t("chat.memberTask.title")}
      </p>
      {items.map((item, index) => (
        <MemberTaskRow
          key={item.id}
          index={index}
          item={item}
          employees={employees}
          defaultOpen={items.length === 1}
        />
      ))}
    </div>
  );
}
