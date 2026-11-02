/**
 * 功能：项目面板宽度钳制契约（拖拽上限收到 224）。
 * 钉两条承诺：① 上限 = 自适应最大宽度（拖不出去超过 224）；② 旧持久化 override
 * （上限收紧前拖到过 320）读时同样被钳制——不靠迁移兜底，绕过持久化直接写 store 也一样。
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  clampProjectPanelWidth,
  PROJECT_PANEL_WIDTH_DEFAULT,
  PROJECT_PANEL_WIDTH_MIN,
  useUiStore,
} from "@/stores/ui-store";

describe("项目面板宽度钳制", () => {
  beforeEach(() => {
    useUiStore.setState({ projectPanelWidthOverride: null });
  });

  it("拖拽写入与纯函数钳制同口径（上限 = DEFAULT 224）", () => {
    expect(PROJECT_PANEL_WIDTH_DEFAULT).toBe(224);
    expect(clampProjectPanelWidth(100)).toBe(PROJECT_PANEL_WIDTH_MIN);
    expect(clampProjectPanelWidth(224)).toBe(224);
    expect(clampProjectPanelWidth(320)).toBe(224);
    useUiStore.getState().setProjectPanelWidth(320);
    expect(useUiStore.getState().projectPanelWidthOverride).toBe(224);
  });

  it("破坏者：旧持久化 override 超上限时读时钳制（不靠 store migrate）", () => {
    useUiStore.setState({ projectPanelWidthOverride: 300 });
    expect(clampProjectPanelWidth(useUiStore.getState().projectPanelWidthOverride ?? 0)).toBe(224);
    useUiStore.getState().setProjectPanelWidth(90);
    expect(useUiStore.getState().projectPanelWidthOverride).toBe(PROJECT_PANEL_WIDTH_MIN);
  });
});
