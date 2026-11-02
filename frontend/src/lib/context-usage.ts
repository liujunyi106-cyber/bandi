/**
 * 功能：上下文用量的数字格式化（组件与测试共用，避免组件文件导出非组件符号）。
 */

/** 功能：比例钳制到 [0,1]（后端已钳，前端再钳一次防跨版本口径漂移画出越界弧）。 */
export function clampRatio(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/** 功能：token 展示——千位折成 k（1000k 即 1M 窗口，按产品口径不折成 M）。 */
export function formatTokenK(value: number): string {
  const safe = Number.isFinite(value) && value > 0 ? value : 0;
  if (safe < 1000) return String(Math.round(safe));
  const k = safe / 1000;
  return `${k >= 100 ? k.toFixed(0) : k.toFixed(1)}k`;
}

/** 功能：占比百分数——一位小数（各行相加等于大号百分比，不出现「加起来对不上」）。 */
export function formatRatioPercent(ratio: number): string {
  return `${(clampRatio(ratio) * 100).toFixed(1)}%`;
}
