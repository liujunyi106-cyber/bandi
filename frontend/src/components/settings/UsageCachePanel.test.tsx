/**
 * 功能：「用量与缓存」面板测试——纯展示组件，直接给数据断言渲染。
 * 重点：数字格式化口径、空态、按项目/按模型明细、命中率越界钳制（破坏者）。
 * ：产品面不展示金额 ⇒ 成本断言移除，改钉「按项目」。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { UsageCachePanel } from "@/components/settings/UsageCachePanel";
import { formatHitRate, formatTokens } from "@/components/settings/usage-format";
import { EMPTY_USAGE_STATS, type UsageStats } from "@/services/settings";

function stats(overrides: Partial<UsageStats["total"]> = {}): UsageStats {
  return {
    ...EMPTY_USAGE_STATS,
    total: {
      ...EMPTY_USAGE_STATS.total,
      requests: 10,
      input_tokens: 1_234_567,
      output_tokens: 8_900,
      cache_read_tokens: 1_000_000,
      cache_creation_tokens: 0,
      cache_hit_rate: 0.812,
      ...overrides,
    },
  };
}

describe("usage-format", () => {
  it("tokens 千分位 / 命中率百分数", () => {
    expect(formatTokens(1_234_567)).toBe("1,234,567");
    expect(formatHitRate(0.812)).toBe("81.2%");
  });

  it("破坏者：非有限值与越界命中率不得渲染成 NaN / 5300%", () => {
    expect(formatTokens(Number.NaN)).toBe("0");
    expect(formatHitRate(53)).toBe("100.0%");
    expect(formatHitRate(-1)).toBe("0.0%");
  });
});

describe("UsageCachePanel（用量 + 缓存同一页签）", () => {
  it("空态：无请求时只给提示，不渲染任何卡片", () => {
    render(
      <UsageCachePanel stats={{ ...EMPTY_USAGE_STATS }} range="all" onRangeChange={vi.fn()} />,
    );
    expect(screen.getByTestId("usage-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-total-input")).not.toBeInTheDocument();
    expect(screen.queryByTestId("cache-read")).not.toBeInTheDocument();
  });

  it("有数据：指标卡 + 缓存明细 + 按模型/按项目行**同屏**（合并页签的核心断言）", () => {
    render(
      <UsageCachePanel
        stats={{
          ...stats(),
          by_model: { "deepseek-v4.1-flash": { ...EMPTY_USAGE_STATS.total, input_tokens: 9 } },
          by_project: { 选品项目: { ...EMPTY_USAGE_STATS.total, input_tokens: 7 } },
        }}
        range="all"
        onRangeChange={vi.fn()}
      />,
    );
    // 用量侧
    expect(screen.getByTestId("usage-total-input")).toHaveTextContent("1,234,567");
    expect(screen.getByTestId("usage-total-output")).toHaveTextContent("8,900");
    expect(screen.queryByTestId("usage-cost")).not.toBeInTheDocument();
    // 缓存侧——**同一页签内**必须同时可见（拆分方案下二者互斥）
    expect(screen.getByTestId("usage-hit-rate")).toHaveTextContent("81.2%");
    expect(screen.getByTestId("cache-read")).toHaveTextContent("1,000,000");
    expect(screen.getByTestId("cache-write")).toHaveTextContent("0");
    expect(screen.getByTestId("cache-requests")).toHaveTextContent("10");
    // 明细
    expect(screen.getByTestId("usage-model-deepseek-v4.1-flash")).toBeInTheDocument();
    expect(screen.getByTestId("usage-project-选品项目")).toBeInTheDocument();
  });

  it("按项目明细：零输入的项目不占位（避免空行噪音）", () => {
    render(
      <UsageCachePanel
        stats={{
          ...stats(),
          by_project: { "(unassigned)": { ...EMPTY_USAGE_STATS.total, input_tokens: 0 } },
        }}
        range="all"
        onRangeChange={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("usage-project-(unassigned)")).not.toBeInTheDocument();
  });

  it("区间切换：点击回调带新区间；当前区间 aria-pressed=true；文案真的解析", () => {
    const onRangeChange = vi.fn();
    render(<UsageCachePanel stats={stats()} range="all" onRangeChange={onRangeChange} />);
    expect(screen.getByTestId("usage-range-all")).toHaveAttribute("aria-pressed", "true");
    // 键名带数字（settings.usage.range.7d）——必须真的解析出文案，不能渲染成裸 key
    expect(screen.getByTestId("usage-range-7d")).toHaveTextContent("7 天");
    fireEvent.click(screen.getByTestId("usage-range-7d"));
    expect(onRangeChange).toHaveBeenCalledWith("7d");
  });

  it("破坏者：越界命中率不得显示成 >100%", () => {
    render(
      <UsageCachePanel stats={stats({ cache_hit_rate: 53 })} range="all" onRangeChange={vi.fn()} />,
    );
    expect(screen.getByTestId("usage-hit-rate")).toHaveTextContent("100.0%");
  });
});
