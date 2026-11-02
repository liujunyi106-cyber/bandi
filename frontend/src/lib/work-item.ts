/**
 * 功能：工作项看板的纯逻辑——列投影、阶段配色、可用动作。
 *
 * 列口径必须与后端 `bandi/company/phase.py::kanban_column` 一致：
 * - 草稿（paused）归「待办」（没在跑）
 * - 失败 / 取消归「已完成」（后端兜底分支），靠红色徽标区分
 *
 * 动作可用性必须与后端端点约束一致（否则点了报 400/404）：
 * - 审阅 review：仅 awaiting_human / awaiting_manager_review，verdict 只接受 approve / rework
 * - 重试 retry：仅 needs_attention（failed 不可重试）
 * - 中止 abort：仅 running（后端要求有活动执行，否则 404）
 */
import { parseTime } from "@/lib/time";
import type { WorkItemSummary } from "@/services/company";

export type KanbanColumnKey = "todo" | "in_progress" | "in_review" | "done";

/** 功能：看板四列的展示顺序与标题 key。 */
export const KANBAN_COLUMNS: Array<{ key: KanbanColumnKey; labelKey: string; color: string }> = [
  { key: "todo", labelKey: "work.column.todo", color: "#9aa1ab" },
  { key: "in_progress", labelKey: "work.column.inProgress", color: "#2f6fd0" },
  { key: "in_review", labelKey: "work.column.inReview", color: "#c08a1e" },
  { key: "done", labelKey: "work.column.done", color: "#1c6b45" },
];

const TODO_PHASES = new Set([
  "queued",
  "ready",
  "waiting_dependencies",
  "ready_for_rework",
  "paused",
]);

const IN_PROGRESS_PHASES = new Set([
  "running",
  "waiting_for_peer",
  "waiting_for_children",
  "needs_attention",
]);

const IN_REVIEW_PHASES = new Set(["awaiting_manager_review", "awaiting_human"]);

const REVIEW_PHASES = new Set(["awaiting_human", "awaiting_manager_review"]);

/** 功能：phase → 看板列（与后端 kanban_column 同口径）。 */
export function columnOfPhase(phase: string): KanbanColumnKey {
  if (TODO_PHASES.has(phase)) return "todo";
  if (IN_PROGRESS_PHASES.has(phase)) return "in_progress";
  if (IN_REVIEW_PHASES.has(phase)) return "in_review";
  return "done";
}

/** 功能：已知 phase 集合（i18n 有对应文案；未知值原样显示，不回落到 key）。 */
const KNOWN_PHASES = new Set([
  "queued",
  "ready",
  "waiting_dependencies",
  "running",
  "waiting_for_peer",
  "waiting_for_children",
  "paused",
  "needs_attention",
  "awaiting_manager_review",
  "awaiting_human",
  "approved",
  "failed",
  "ready_for_rework",
  "cancelled",
]);

/** 功能：phase → 文案 key；未知 phase 返回空串（调用方直接显示原值）。 */
export function phaseLabelKey(phase: string): string {
  return KNOWN_PHASES.has(phase) ? `work.phase.${phase}` : "";
}

/** 功能：按列分组，保持后端返回顺序；四列都返回（空列也要画空态）。 */
export function groupByColumn<T extends WorkItemSummary>(
  items: T[],
): Record<KanbanColumnKey, T[]> {
  const grouped: Record<KanbanColumnKey, T[]> = {
    todo: [],
    in_progress: [],
    in_review: [],
    done: [],
  };
  for (const item of items) {
    grouped[columnOfPhase(item.phase)].push(item);
  }
  return grouped;
}

/** 功能：阶段徽标配色（失败/取消用红系，草稿用紫系，避免与「已通过」混看）。 */
export function phaseTone(phase: string): string {
  if (phase === "failed") return "bg-[#fbeceb] text-[#a32d2d]";
  if (phase === "cancelled") return "bg-[#f2f4f6] text-[#6b7280]";
  if (phase === "needs_attention") return "bg-[#fdf1ec] text-[#9a3b1c]";
  if (phase === "paused") return "bg-[#f4f1fd] text-[#4b3d94] border border-[#e2dcf7]";
  if (phase === "running") return "bg-[#eaf3ff] text-[#1f4f96]";
  if (phase === "waiting_for_peer" || phase === "waiting_for_children") {
    return "bg-[#f1effc] text-[#4b3d94]";
  }
  if (REVIEW_PHASES.has(phase)) return "bg-[#fdf4e3] text-[#8a5a10]";
  if (phase === "ready_for_rework") return "bg-[#fdf4e3] text-[#8a5a10]";
  if (phase === "approved") return "bg-[#eaf5ee] text-[#1c6b45]";
  if (phase === "waiting_dependencies") return "bg-[#f6f2e8] text-[#8a6a1f]";
  return "bg-[#f2f4f6] text-[#5c636b]";
}

export interface WorkItemActions {
  canReview: boolean;
  canRetry: boolean;
  canAbort: boolean;
}

/** 功能：按 phase 判定动作是否可用（与后端端点约束一一对应）。 */
export function actionsOf(item: Pick<WorkItemSummary, "phase">): WorkItemActions {
  return {
    canReview: REVIEW_PHASES.has(item.phase),
    canRetry: item.phase === "needs_attention",
    canAbort: item.phase === "running",
  };
}

/** 功能：动作是否全不可用（卡片展开区据此显示说明文案）。 */
export function hasAnyAction(actions: WorkItemActions): boolean {
  return actions.canReview || actions.canRetry || actions.canAbort;
}

/** 功能：筛选（团队/岗位/关键词）——团队按工作项 id 前缀推断，缺省不过滤。 */
export function filterItems<T extends WorkItemSummary>(
  items: T[],
  filter: { query?: string; role?: string; teamRoleIds?: ReadonlySet<string> },
): T[] {
  const keyword = (filter.query ?? "").trim().toLowerCase();
  return items.filter((item) => {
    if (
      filter.teamRoleIds &&
      filter.teamRoleIds.size > 0 &&
      !filter.teamRoleIds.has(item.assignee_role)
    ) {
      return false;
    }
    if (filter.role && item.assignee_role !== filter.role) return false;
    if (!keyword) return true;
    const haystack = `${item.title} ${item.id}`.toLowerCase();
    return haystack.includes(keyword);
  });
}

/** 功能：时间显示（ISO → 本地）；`withDate` 时带月日，解析失败返回空串。 */
export function formatTime(value?: string | Date, withDate = false): string {
  const date = parseTime(value);
  if (!date) return "";
  return date.toLocaleString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    ...(withDate ? { month: "2-digit", day: "2-digit" } : {}),
  });
}

/** 功能：按更新时间倒序；同刻保持后端原始顺序（= 创建顺序）。 */
export function sortByUpdated<T extends WorkItemSummary>(items: T[]): T[] {
  return [...items].sort((left, right) => {
    const a = left.updated_at ?? left.created_at ?? "";
    const b = right.updated_at ?? right.created_at ?? "";
    if (a === b) return 0;
    return a < b ? 1 : -1;
  });
}
