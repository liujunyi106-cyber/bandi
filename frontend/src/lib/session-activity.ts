/**
 * 功能：会话活动态聚合——把「一组会话 id」归并成单个状态，供项目行/头像行使用。
 *
 * 语义与单条会话行一致：`running` 优先于 `done`（有任一在跑就显示在跑）；
 * 跳过当前会话（它自身不显示状态点——用户正看着它）。
 * 数据源 = `chat-store.sessionActivity`（发送时置 running / 完成帧置 done）。
 */
import type { SessionActivity } from "@/stores/chat-store";

export type ActivityState = "running" | "done" | "";

export function aggregateSessionActivity(
  sessionIds: readonly string[],
  activity: Record<string, SessionActivity>,
  skipSessionId?: string | null,
): ActivityState {
  let done = false;
  for (const id of sessionIds) {
    if (skipSessionId && id === skipSessionId) continue;
    const state = activity[id];
    if (state === "running") return "running";
    if (state === "done") done = true;
  }
  return done ? "done" : "";
}

/** 功能：是否已读（done 才清除；running 不得因打开会话被抹掉）。 */
export function shouldClearOnOpen(state: SessionActivity | undefined): boolean {
  return state === "done";
}

/**
 * 功能：员工单聊行状态——按 (项目, 角色) 扫描员工过程槽键。
 *
 * 单聊是线程制（无 session id），状态源只能是过程槽：键形如
 * `${projectScope}::${threadId}::${roleId}`（见 chat-store employeeProgress）。
 * 同一员工可能有多个历史线程 → 任一在跑即「执行中」，否则任一完成即「已完成」。
 */
export function aggregateDmActivity(
  projectScope: string,
  roleId: string,
  progress: Record<string, { length: number }>,
  done: Record<string, boolean>,
): ActivityState {
  if (!projectScope || !roleId) return "";
  const prefix = `${projectScope}::`;
  const suffix = `::${roleId}`;
  let completed = false;
  for (const key of Object.keys(progress)) {
    if (!key.startsWith(prefix) || !key.endsWith(suffix)) continue;
    if (done[key] === true) {
      completed = true;
      continue;
    }
    if ((progress[key]?.length ?? 0) > 0) return "running";
  }
  if (completed) return "done";
  // 过程槽为空但已完成标记仍在（过期前窗口）也要显示完成
  for (const key of Object.keys(done)) {
    if (key.startsWith(prefix) && key.endsWith(suffix) && done[key] === true) return "done";
  }
  return "";
}
