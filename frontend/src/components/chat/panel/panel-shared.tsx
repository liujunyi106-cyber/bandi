/**
  * 功能：右栏共享展示件（v5）——概览摘要卡与员工卡共用（@base docs/前端设计规划.md）。
 * 高密度 product register：hairline 圆角小格 + 数字主值 + 可选副注。
 */

/** 功能：token 数紧凑显示（1.2k / 34.5k / 1.2M）。 */
export function compactTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

/** 功能：摘要小格（标签 + 主值 + 可选副注）。 */
export function SummaryCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="border-chat-hairline bg-chat-rail/40 rounded-lg border px-2.5 py-2">
      <p className="text-chat-muted text-[11px]">{label}</p>
      <p className="text-chat-ink mt-0.5 truncate text-sm font-semibold tabular-nums">{value}</p>
      {hint ? <p className="text-chat-muted truncate text-[10px]">{hint}</p> : null}
    </div>
  );
}
