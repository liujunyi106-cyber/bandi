/**
 * 功能：面板宽度随窗口自适应测试（锚点/插值/clamp）。
 */
import { describe, expect, it } from "vitest";

import {
  ADAPTIVE_WINDOW_FULL,
  ADAPTIVE_WINDOW_MIN,
  ADAPTIVE_WINDOW_TIGHT,
  adaptiveProjectPanelWidth,
  adaptiveRightPanelWidth,
  adaptiveSidebarWidth,
  SIDEBAR_WIDTH_TIGHT,
} from "@/lib/panel-width";

describe("panel-width 自适应", () => {
  it("锚点：最小窗口 1024 → 最小值；全屏 1920 → 默认值", () => {
    expect(adaptiveRightPanelWidth(1024)).toBe(260);
    expect(adaptiveRightPanelWidth(1920)).toBe(400);
    expect(adaptiveProjectPanelWidth(1024)).toBe(160);
    expect(adaptiveProjectPanelWidth(1920)).toBe(224);
    expect(adaptiveSidebarWidth(1024)).toBe(184);
    expect(adaptiveSidebarWidth(1920)).toBe(216);
  });

  it("中间线性插值（1440 约四成六分）", () => {
    expect(adaptiveRightPanelWidth(1440)).toBe(325);
    expect(adaptiveProjectPanelWidth(1440)).toBe(190);
    expect(adaptiveSidebarWidth(1440)).toBe(199);
  });

  it("破坏者：窗口小于/大于两端锚点一律 clamp（不越界）", () => {
    expect(ADAPTIVE_WINDOW_MIN).toBe(1024);
    expect(ADAPTIVE_WINDOW_FULL).toBe(1920);
    expect(adaptiveRightPanelWidth(0)).toBe(260);
    expect(adaptiveRightPanelWidth(1023)).toBe(260);
    expect(adaptiveRightPanelWidth(99999)).toBe(400);
    expect(adaptiveProjectPanelWidth(0)).toBe(160);
    expect(adaptiveProjectPanelWidth(99999)).toBe(224);
    expect(adaptiveSidebarWidth(99999)).toBe(216);
  });

  /**
   * 功能：窄窗（<1024）左栏**保持展开**并继续缩窄。
   * 背景：原实现 980px 断点走抽屉分支 → 左栏整体消失（「只剩会话」）
   * 且宽度在 1024 以下恒为 184（不再缩）。现口径：640 触底 152，不再回落 184。
   */
  it("窄窗 1024→640 继续缩窄 184→152；≤640 触底不再缩", () => {
    expect(ADAPTIVE_WINDOW_TIGHT).toBe(640);
    expect(SIDEBAR_WIDTH_TIGHT).toBe(152);
    expect(adaptiveSidebarWidth(1024)).toBe(184);
    expect(adaptiveSidebarWidth(832)).toBe(168);
    expect(adaptiveSidebarWidth(640)).toBe(152);
  });

  it("破坏者：窄窗不得回落 184（=左栏消失前的旧 clamp），也不得为 0", () => {
    expect(adaptiveSidebarWidth(320)).toBe(152);
    expect(adaptiveSidebarWidth(0)).toBe(152);
    // 单调不增：越窄越不大，且始终 ≥ 触底值
    expect(adaptiveSidebarWidth(500)).toBeLessThanOrEqual(adaptiveSidebarWidth(800));
    expect(adaptiveSidebarWidth(500)).toBeGreaterThanOrEqual(SIDEBAR_WIDTH_TIGHT);
  });
});
