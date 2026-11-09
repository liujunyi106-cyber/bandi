/**
 * 功能：用量/缓存页签的数字格式化。集中一处，避免各组件各写一遍口径。
 */

/** 功能：tokens 千分位（后端给的是整数；此处只做展示）。 */
export function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(
    Number.isFinite(value) ? value : 0,
  );
}

/** 功能：命中率百分数（后端已钳制 [0,1]；此处再钳一次，防跨协议口径漂移显示成 5300%）。 */
export function formatHitRate(value: number): string {
  const safe = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;
  return `${(safe * 100).toFixed(1)}%`;
}
