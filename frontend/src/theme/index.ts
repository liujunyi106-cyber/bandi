/**
 * 功能：主题辅助。
 * 色板与会话区子色板全部以 CSS 变量落在 `src/index.css`（Tailwind v4 `@theme`）
 * 这里只放需要在 TS 里做映射的少量派生值（员工 tone、危险级配色）。
 */
import type { DangerLevel, EmployeeTone } from "@/types";

/** 员工角色视觉身份 → Tailwind 类。 */
export const TONE_CLASS: Record<EmployeeTone, string> = {
  ink: "bg-tone-ink text-white",
  copper: "bg-tone-copper text-white",
  olive: "bg-tone-olive text-white",
  graphite: "bg-tone-graphite text-white",
};

/** 危险操作分级 → 视觉语气。 */
export const DANGER_CLASS: Record<DangerLevel, string> = {
  L1: "text-muted-foreground",
  L2: "text-warning",
  L3: "text-destructive",
};

/** 功能：取员工 tone 类名。 */
export function toneClass(tone: EmployeeTone): string {
  return TONE_CLASS[tone];
}
