/**
 * 功能：Composer 上下文用量环——灰色空心圆 + 黑色进度（12 点方向起顺时针），点击展开
 * 「上下文用量」面板：大号百分比、已使用 k / 窗口 k、五色进度条与分类图例。
 *
 * 数据来自 `GET /api/sessions/{id}/context-usage`（agent 每次组装请求时落库的那一份），
 * 因此环上显示的就是**当前这一轮真正发出去的上下文**；无快照时显式显示「暂无」而不是伪造 0。
 */
import { X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/i18n";
import { clampRatio, formatRatioPercent, formatTokenK } from "@/lib/context-usage";
import { cn } from "@/lib/utils";
import { sessionsApi, type ContextUsage, type ContextUsageBucketKey } from "@/services/sessions";

/** 五类口径的展示顺序 + 颜色（复用图表色板，零新增颜色）。 */
const BUCKETS: { key: ContextUsageBucketKey; color: string; labelKey: string }[] = [
  { key: "system", color: "var(--chart-1)", labelKey: "chat.contextUsage.system" },
  { key: "tools", color: "var(--chart-2)", labelKey: "chat.contextUsage.tools" },
  { key: "messages", color: "var(--chart-3)", labelKey: "chat.contextUsage.messages" },
  { key: "connectors", color: "var(--chart-4)", labelKey: "chat.contextUsage.connectors" },
  { key: "skills", color: "var(--chart-5)", labelKey: "chat.contextUsage.skills" },
];

const RING_RADIUS = 7.6;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

interface ContextUsageRingProps {
  /** 会话 id（空串 = 未选中会话，环显示 0 且不请求）。 */
  sessionId: string;
  /** 递增信号：一轮结束（busy 由真变假）时触发重取。 */
  refreshKey?: number;
}

export function ContextUsageRing({ sessionId, refreshKey = 0 }: ContextUsageRingProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [usage, setUsage] = useState<ContextUsage | null>(null);
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  const load = useCallback(async () => {
    if (!sessionId) {
      setUsage(null);
      return;
    }
    try {
      const next = await sessionsApi.contextUsage(sessionId);
      // 会话在请求途中被切换时丢弃旧会话的响应（避免环显示上一个会话的数字）
      if (sessionRef.current !== sessionId) return;
      setUsage(next);
    } catch {
      if (sessionRef.current === sessionId) setUsage(null);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  // 无快照（重启后尚未跑过一轮 / 老会话）时显示占位符而不是 0.0%——
  // 「0」是真实测量值，与「还没测过」必须区分开
  const measured = Boolean(usage?.available);
  const ratio = clampRatio(usage?.ratio ?? 0);
  const buckets = BUCKETS.map((meta) => {
    const found = usage?.buckets.find((item) => item.key === meta.key);
    return { ...meta, tokens: found?.tokens ?? 0, ratio: clampRatio(found?.ratio ?? 0) };
  });

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="context-usage-ring"
          data-ratio={ratio.toFixed(4)}
          aria-label={t("chat.contextUsage.open")}
          title={t("chat.contextUsage.open")}
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-full transition-colors",
            "hover:bg-primary/5 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
          )}
        >
          <svg viewBox="0 0 20 20" className="size-5 -rotate-90" aria-hidden="true">
            <circle
              cx="10"
              cy="10"
              r={RING_RADIUS}
              fill="none"
              stroke="var(--chat-hairline)"
              strokeWidth="2.4"
            />
            {ratio > 0 ? (
              <circle
                data-testid="context-usage-ring-progress"
                cx="10"
                cy="10"
                r={RING_RADIUS}
                fill="none"
                stroke="var(--chat-ink)"
                strokeWidth="2.4"
                strokeLinecap="round"
                strokeDasharray={RING_CIRCUMFERENCE}
                strokeDashoffset={RING_CIRCUMFERENCE * (1 - ratio)}
              />
            ) : null}
          </svg>
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        sideOffset={8}
        data-testid="context-usage-panel"
        className="border-chat-hairline bg-card w-64 gap-3 rounded-xl border p-3 shadow-md ring-0"
      >
        <div className="flex items-center justify-between gap-2">
          <p className="text-chat-muted text-[11px]">{t("chat.contextUsage.title")}</p>
          <button
            type="button"
            data-testid="context-usage-close"
            aria-label={t("chat.contextUsage.close")}
            title={t("chat.contextUsage.close")}
            onClick={() => setOpen(false)}
            className="text-chat-muted hover:bg-primary/5 hover:text-chat-ink -mr-1 flex size-5 items-center justify-center rounded-full transition-colors"
          >
            <X className="size-3.5" />
          </button>
        </div>
        <div className="flex items-baseline justify-between gap-2">
          <span
            data-testid="context-usage-percent"
            className="text-chat-ink text-[22px] font-semibold tabular-nums"
          >
            {measured ? formatRatioPercent(ratio) : "—"}
          </span>
          <span className="text-chat-muted text-[11px] tabular-nums">
            {measured
              ? `${t("chat.contextUsage.used")} ${formatTokenK(usage?.used_tokens ?? 0)} / ${formatTokenK(usage?.context_window ?? 0)}`
              : `${t("chat.contextUsage.used")} — / —`}
          </span>
        </div>
        <div
          data-testid="context-usage-bar"
          className="bg-chat-hairline flex h-1.5 w-full overflow-hidden rounded-full"
        >
          {buckets.map((bucket) =>
            bucket.ratio > 0 ? (
              <span
                key={bucket.key}
                data-testid={`context-usage-bar-${bucket.key}`}
                style={{ width: `${bucket.ratio * 100}%`, background: bucket.color }}
              />
            ) : null,
          )}
        </div>
        <ul className="flex flex-col gap-1.5">
          {buckets.map((bucket) => (
            <li key={bucket.key} className="flex items-center gap-2 text-[11px]">
              <span
                aria-hidden="true"
                className="size-1.5 shrink-0 rounded-full"
                style={{ background: bucket.color }}
              />
              <span className="text-chat-muted min-w-0 flex-1 truncate">{t(bucket.labelKey)}</span>
              <span className="text-chat-ink tabular-nums">
                {measured ? formatRatioPercent(bucket.ratio) : "—"}
              </span>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
