import { describe, expect, it } from "vitest";

import { cn, formatDuration } from "@/lib/utils";

describe("cn", () => {
  it("合并条件类并消解 Tailwind 冲突（后者胜）", () => {
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("text-sm", false && "hidden", "font-medium")).toBe("text-sm font-medium");
  });
});

describe("formatDuration", () => {
  it("毫秒 / 秒 / 分 / 时四档按阈值切换", () => {
    expect(formatDuration(0)).toBe("0ms");
    expect(formatDuration(320)).toBe("320ms");
    expect(formatDuration(1_500)).toBe("1.5s");
    expect(formatDuration(45_000)).toBe("45s");
    expect(formatDuration(90_000)).toBe("1m30s");
    expect(formatDuration(7_200_000)).toBe("2h0m");
  });

  it("非法输入返回占位符而非 NaN", () => {
    expect(formatDuration(Number.NaN)).toBe("—");
    expect(formatDuration(-1)).toBe("—");
    expect(formatDuration(Number.POSITIVE_INFINITY)).toBe("—");
  });
});
