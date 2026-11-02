/**
  * 功能：主题辅助（@base docs/前端设计规划.md 视觉 token / 主题范围）。
 * 色板与会话区子色板全部以 CSS 变量落在 `src/index.css`（Tailwind v4 `@theme`）
 */
import type { DangerLevel, EmployeeTone } from "@/types";

/* 员工角色视觉身份 → Tailwind 类（员工 tone；第 4 档石墨灰， 去蓝）。 */
export const TONE_CLASS: Record<EmployeeTone, string> = {
  ink: "bg-tone-ink text-white",
  copper: "bg-tone-copper text-white",
  olive: "bg-tone-olive text-white",
  graphite: "bg-tone-graphite text-white",
};

/* 危险操作分级 → 视觉语气（危险操作分级确认）。 */
export const DANGER_CLASS: Record<DangerLevel, string> = {
  L1: "text-muted-foreground",
  L2: "text-warning",
  L3: "text-destructive",
};

/** 功能：取员工 tone 类名。 */
export function toneClass(tone: EmployeeTone): string {
  return TONE_CLASS[tone];
}
