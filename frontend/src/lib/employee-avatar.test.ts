import { describe, expect, it } from "vitest";

import { AVATAR_TONE_CLASSES, hashTone } from "@/lib/employee-avatar";

describe("hashTone", () => {
  it("同一姓名稳定命中同一挡位", () => {
    expect(hashTone("林知远")).toBe(hashTone("林知远"));
    expect(hashTone("alpha")).toBe(hashTone("alpha"));
  });

  it("结果永远落在预设挡位内", () => {
    for (const seed of ["林知远", "周予安", "a", "", "产品设计师", "12345"]) {
      const tone = hashTone(seed);
      expect(tone).toBeGreaterThanOrEqual(0);
      expect(tone).toBeLessThan(AVATAR_TONE_CLASSES.length);
      expect(Number.isInteger(tone)).toBe(true);
    }
  });

  it("不同姓名能分散到不同挡位（不全部撞成 0）", () => {
    const tones = new Set(["林知远", "周予安", "陈嘉禾", "苏见月", "何清和"].map(hashTone));
    expect(tones.size).toBeGreaterThan(1);
  });
});
