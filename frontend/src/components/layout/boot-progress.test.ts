// 功能：契约——启动阶段到进度百分比的映射单调、可解释，且覆盖整条启动链路。
import { describe, expect, it } from "vitest";

import {
  BOOT_STAGE_PROGRESS,
  WARMUP_PROGRESS,
  bootProgressOf,
  type BootStage,
} from "@/components/layout/boot-progress";

describe("启动阶段进度口径", () => {
  it("四个真实阶段各自对应固定百分比，且严格递增", () => {
    const stages: BootStage[] = ["spawn", "listening", "probing", "ready"];
    const values = stages.map((stage) => BOOT_STAGE_PROGRESS[stage]);

    expect(values).toEqual([...values].sort((a, b) => a - b));
    expect(new Set(values).size).toBe(values.length);
    expect(values[0]).toBeGreaterThan(0);
    // 内核就绪不是界面可用：留余量给首屏预热
    expect(values[values.length - 1]).toBeLessThan(100);
  });

  it("内核就绪停在 85%，首屏预热把它推到 100%", () => {
    expect(BOOT_STAGE_PROGRESS.ready).toBe(85);
    expect(WARMUP_PROGRESS.start).toBe(BOOT_STAGE_PROGRESS.ready);
    expect(WARMUP_PROGRESS.end).toBe(100);
    for (const stage of ["spawn", "listening", "probing"] as const) {
      expect(BOOT_STAGE_PROGRESS[stage]).toBeLessThan(85);
    }
  });

  it("阶段内按亚进度插值：同阶段不超过下一阶段起点", () => {
    expect(bootProgressOf("spawn", 0)).toBe(BOOT_STAGE_PROGRESS.spawn);
    expect(bootProgressOf("spawn", 1)).toBeLessThan(BOOT_STAGE_PROGRESS.listening);

    const half = bootProgressOf("listening", 0.5);
    expect(half).toBeGreaterThan(BOOT_STAGE_PROGRESS.listening);
    expect(half).toBeLessThan(BOOT_STAGE_PROGRESS.probing);
  });

  it("亚进度越界收敛到 [0,1]，不产生负值或超界", () => {
    expect(bootProgressOf("probing", -5)).toBe(BOOT_STAGE_PROGRESS.probing);
    expect(bootProgressOf("probing", 9)).toBeLessThanOrEqual(100);
    expect(bootProgressOf("probing", 9)).toBeGreaterThan(BOOT_STAGE_PROGRESS.probing);
  });

  it("同为就绪阶段时亚进度不影响结果", () => {
    expect(bootProgressOf("ready", 0)).toBe(85);
    expect(bootProgressOf("ready", 1)).toBe(85);
  });

  it("未知阶段回落最小可见进度而非 100%（不得假装就绪）", () => {
    expect(bootProgressOf("unknown" as BootStage, 0)).toBe(BOOT_STAGE_PROGRESS.spawn);
  });
});
