/**
 * 功能：消息时间格式化测试（HH:mm 常显 / 精确时间 / 相对日期 / 空值防御）。
 */
import { describe, expect, it } from "vitest";

import { formatClock, formatFullTime, formatRelativeTime } from "@/lib/time";

describe("formatClock", () => {
  it("ISO 时间 → HH:mm（24 小时制补零）", () => {
    expect(formatClock("2026-09-23T09:05:00")).toBe("09:05");
    expect(formatClock("2026-09-23T23:59:00")).toBe("23:59");
  });

  it("空值/非法值 → 空串（调用方跳过渲染）", () => {
    expect(formatClock("")).toBe("");
    expect(formatClock(undefined)).toBe("");
    expect(formatClock(null)).toBe("");
    expect(formatClock("not-a-date")).toBe("");
  });
});

describe("formatFullTime", () => {
  it("精确到秒（YYYY-MM-DD HH:mm:ss）", () => {
    expect(formatFullTime("2026-09-23T09:05:07")).toBe("2026-09-23 09:05:07");
  });

  it("空值 → 空串", () => {
    expect(formatFullTime(undefined)).toBe("");
    expect(formatFullTime("garbage")).toBe("");
  });
});

describe("formatRelativeTime", () => {
  const now = new Date("2026-09-25T18:00:00");

  it("今天 → 仅 HH:mm", () => {
    expect(formatRelativeTime("2026-09-25T09:12:00", now)).toBe("09:12");
    expect(formatRelativeTime("2026-09-25T17:59:00", now)).toBe("17:59");
  });

  it("昨天 / 前天文案", () => {
    expect(formatRelativeTime("2026-09-24T22:05:00", now)).toBe("昨天 22:05");
    expect(formatRelativeTime("2026-09-23T01:00:00", now)).toBe("前天 01:00");
  });

  it("更早（同年）→ M月D日 HH:mm；跨年 → 带年份", () => {
    expect(formatRelativeTime("2026-09-20T14:30:00", now)).toBe("9月20日 14:30");
    expect(formatRelativeTime("2025-12-31T23:59:00", now)).toBe("2025年12月31日 23:59");
  });

  it("按本地自然日计算（不受时刻影响：昨天 23:59 仍是昨天）", () => {
    expect(formatRelativeTime("2026-09-24T23:59:00", now)).toBe("昨天 23:59");
  });

  it("空值/非法 → 空串", () => {
    expect(formatRelativeTime("", now)).toBe("");
    expect(formatRelativeTime("garbage", now)).toBe("");
  });
});
