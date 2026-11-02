/**
 * 功能：时间解析与格式化唯一入口。
 * 后端时间口径：存储为 UTC 带偏移串；接口层已统一输出带本地时区偏移的 ISO 串。
 * 解析规则：带 `Z`/`±HH:MM` 按偏移换算；缺偏移按本地时间读（兼容历史本地 naive 串）。
 * 空值/非法值返回空串（调用方按空串跳过渲染）。
 */

/** 功能：字符串时间戳归一（空格分隔转 ISO 的 T，提升跨浏览器解析稳定性）。 */
export function parseTime(value?: string | number | Date | null): Date | null {
  if (value === null || value === undefined || value === "") return null;
  const normalized =
    typeof value === "string" ? value.trim().replace(" ", "T") : value;
  const date = normalized instanceof Date ? normalized : new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** 功能：时钟时间（HH:mm，24 小时制）——消息头/操作条常显。 */
export function formatClock(value?: string | number | Date | null): string {
  const date = parseTime(value);
  if (!date) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** 功能：相对时间（今天→HH:mm / 昨天→昨天 HH:mm / 前天→前天 HH:mm / 本年→M月D日 HH:mm /
 * 跨年→YYYY年M月D日 HH:mm）——按本地自然日计算（需含昨天等日期信息）。 */
export function formatRelativeTime(
  value?: string | number | Date | null,
  now: Date = new Date(),
): string {
  const date = parseTime(value);
  if (!date) return "";
  const clock = formatClock(date);
  const startOfDay = (input: Date) =>
    new Date(input.getFullYear(), input.getMonth(), input.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  if (dayDiff <= 0) return clock;
  if (dayDiff === 1) return `昨天 ${clock}`;
  if (dayDiff === 2) return `前天 ${clock}`;
  if (date.getFullYear() === now.getFullYear()) {
    return `${date.getMonth() + 1}月${date.getDate()}日 ${clock}`;
  }
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${clock}`;
}

/** 功能：精确时间（YYYY-MM-DD HH:mm:ss）——title 悬停展示。 */
export function formatFullTime(value?: string | number | null): string {
  const date = parseTime(value);
  if (!date) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}
