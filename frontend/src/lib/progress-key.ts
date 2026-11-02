/**
 * 功能：员工过程槽的键构造（统一口径）。
 *
 * 背景：过程槽键原散落 8 处各写各的——host 写侧用 `String(data.project_id ?? "")`
 * 单聊过滤/消费用 `projectId || "company"`、群聊过滤用 `?? "company"` 而键前缀用原始值
 * store 内 5 处用原始值。今天不分叉只因为后端保证 project 恒非空（见下）
 * 一旦出现空 project 帧，「写进的键」与「读的键」就会错开 ⇒ 过程块消费不到。
 *
 * 归一化取值 = `"company"`：与 API dispatcher 的硬编码口径一致
 * （`app.py` 装配 `CommunicationManager(project_id="company")`），也是当前唯一的生产值。
 * ⚠️ 注意后端 `_build_runtime` 的**最终**兜底是 `"default"`（工作项归属 > communication > "default"）
 * 二者只在「不可达的空值路径」上不同；若将来引入非 company 的默认项目，本常量须改为镜像后端。
 *
 * （单一键构造器 + 统一 `strip()` 归一化）。
 */

/** 空项目作用域回退值（与 API dispatcher 口径一致）。 */
export const DEFAULT_PROJECT_SCOPE = "company";

/** 功能：把任意来源的 project 值归一化为槽键可用的作用域（空/空白 → 回退值）。 */
export function normalizeProjectScope(projectId?: string | null): string {
  return (projectId ?? "").trim() || DEFAULT_PROJECT_SCOPE;
}

/** 功能：过程槽键（`<scope>::<thread>::<role>`）——写侧与读侧必须都走这里。
 *
 * 为什么带 thread：过程帧与产出回复的 thread_id 同源
 * （`item.metadata["thread_id"]`——回写在 `company_mode.py`、帧归属在 `seat_executor.py`）
 * 因此它是「这一次执行」的天然身份。带上它以后：
 * ① 补发/迟到的**终态帧**只关停它自己那一次的槽，不会误终结同角色「新开的那一次」；
 * ② 断线期间「同一员工跑完 A 又跑完 B」时，两条回复各自配到自己的过程块（不同槽）
 * 不会出现 A 的回复挂 B 的过程（角色键时代二者同槽 ⇒ 只能靠时序侥幸）。
 * thread 缺省空串（工作项无 thread 时后端不产出回复，槽自然无人消费）。
 */
export function progressKey(
  projectId: string | null | undefined,
  threadId: string | null | undefined,
  roleId: string,
): string {
  return `${normalizeProjectScope(projectId)}::${threadId ?? ""}::${roleId}`;
}
