/**
 * 功能：设置弹窗「用量与缓存」页签（@base docs/前端设计规划.md 弹窗模式）。
 *
 * 为什么是**一个**页签：用量与缓存来自**同一份聚合数据**（同一次
 * `GET /api/usage/stats`、同一个本地台账），且「命中率」本身就同时属于两个视角——
 * 拆成两个页签只会造成重复展示与重复请求。故合并为一块可滚动内容：
 * 指标卡（输入/输出/命中率）→ 缓存明细（读/写/请求数）→ 按模型明细 → 按员工明细。
 *
 * ：**产品面不再展示费用**（金额受模型目录/网关加价/缓存分档
 * 影响，口径复杂且常被误读；token 与命中率是无歧义硬事实）。缓存只在设置页看
 * 右栏只显示 token。
 *
 * 纯展示组件：数据由 SettingsDialog 统一取一次后传入。
 */
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { UsageBucket, UsageRange, UsageStats } from "@/services/settings";

import { formatHitRate, formatTokens } from "@/components/settings/usage-format";

const RANGES: UsageRange[] = ["7d", "30d", "all"];

interface UsageCachePanelProps {
  stats: UsageStats;
  range: UsageRange;
  onRangeChange: (range: UsageRange) => void;
  loading?: boolean;
}

/** 功能：一张指标卡（标签在上、数字在下，仓库既有卡片语言）。 */
function MetricCard({
  label,
  value,
  testId,
  tone,
}: {
  label: string;
  value: string;
  testId: string;
  tone?: "success";
}) {
  return (
    <div className="border-chat-hairline rounded-lg border px-3 py-2.5" data-testid={testId}>
      <p className="text-chat-muted text-[11px]">{label}</p>
      <p className={cn("mt-1 text-lg font-medium", tone === "success" && "text-success")}>
        {value}
      </p>
    </div>
  );
}

/** 功能：缓存明细里的小卡（次级字号，与主指标卡区分层级）。 */
function DetailCard({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="border-chat-hairline rounded-lg border px-3 py-2" data-testid={testId}>
      <p className="text-chat-muted text-[11px]">{label}</p>
      <p className="mt-1 text-sm font-medium">{value}</p>
    </div>
  );
}

/** 功能：分桶明细列表（按模型 / 按员工共用）——名称 · 命中率 · 输入 tokens。 */
function BucketList({
  label,
  entries,
  testIdPrefix,
}: {
  label: string;
  entries: [string, UsageBucket][];
  testIdPrefix: string;
}) {
  return (
    <div>
      <p className="text-chat-muted text-[11px]">{label}</p>
      <ul className="mt-1 space-y-0.5">
        {entries.map(([name, bucket]) => (
          <li
            key={name}
            data-testid={`${testIdPrefix}-${name}`}
            className="border-chat-hairline flex items-center gap-2 border-b py-1.5 text-[11px] last:border-b-0"
          >
            <span className="min-w-0 flex-1 truncate font-mono">{name}</span>
            <span className="text-chat-muted w-14 shrink-0 text-right">
              {formatHitRate(bucket.cache_hit_rate)}
            </span>
            <span className="text-chat-muted w-24 shrink-0 text-right">
              {formatTokens(bucket.input_tokens)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 功能：按输入 tokens 降序取前 N（空桶不列）。 */
function topBuckets(source: Record<string, UsageBucket>, limit = 5): [string, UsageBucket][] {
  return Object.entries(source)
    .filter(([, bucket]) => bucket.input_tokens > 0)
    .sort(([, a], [, b]) => b.input_tokens - a.input_tokens)
    .slice(0, limit);
}

export function UsageCachePanel({
  stats,
  range,
  onRangeChange,
  loading = false,
}: UsageCachePanelProps) {
  const t = useT();
  const total = stats.total;
  const models = topBuckets(stats.by_model);
  const projects = topBuckets(stats.by_project);

  return (
    <div data-testid="settings-panel-usage-cache" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-chat-ink text-xs font-medium">{t("settings.usage.title")}</h3>
          <p className="text-chat-muted mt-0.5 text-[11px]">{t("settings.usage.subtitle")}</p>
        </div>
        <div
          role="group"
          aria-label={t("settings.usage.rangeLabel")}
          className={cn(
            "border-chat-hairline flex overflow-hidden rounded-md border",
            // 加载中只降透明度、**不禁用**：禁用会吞掉用户连点（切区间幂等
            // effect 内已有 cancelled 守卫丢弃过期响应）
            loading && "opacity-60",
          )}
        >
          {RANGES.map((item) => (
            <button
              key={item}
              type="button"
              data-testid={`usage-range-${item}`}
              aria-pressed={item === range}
              onClick={() => onRangeChange(item)}
              className={cn(
                "px-2.5 py-1 text-[11px]",
                item === range ? "bg-primary/5 font-medium" : "text-chat-muted",
              )}
            >
              {t(`settings.usage.range.${item}`)}
            </button>
          ))}
        </div>
      </div>

      {total.requests === 0 ? (
        <p className="text-chat-muted text-xs" data-testid="usage-empty">
          {t("settings.usage.empty")}
        </p>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <MetricCard
              label={t("settings.usage.totalInput")}
              value={formatTokens(total.input_tokens)}
              testId="usage-total-input"
            />
            <MetricCard
              label={t("settings.usage.totalOutput")}
              value={formatTokens(total.output_tokens)}
              testId="usage-total-output"
            />
            <MetricCard
              label={t("settings.usage.hitRate")}
              value={formatHitRate(total.cache_hit_rate)}
              testId="usage-hit-rate"
              tone="success"
            />
          </div>

          <div className="grid grid-cols-3 gap-2">
            <DetailCard
              label={t("settings.cache.read")}
              value={formatTokens(total.cache_read_tokens)}
              testId="cache-read"
            />
            <DetailCard
              label={t("settings.cache.write")}
              value={formatTokens(total.cache_creation_tokens)}
              testId="cache-write"
            />
            <DetailCard
              label={t("settings.cache.requests")}
              value={formatTokens(total.requests)}
              testId="cache-requests"
            />
          </div>

          <BucketList
            label={t("settings.usage.byProject")}
            entries={projects}
            testIdPrefix="usage-project"
          />
          <BucketList
            label={t("settings.usage.byModel")}
            entries={models}
            testIdPrefix="usage-model"
          />
        </>
      )}
    </div>
  );
}
