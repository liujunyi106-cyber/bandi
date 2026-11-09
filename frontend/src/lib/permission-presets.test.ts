import { describe, expect, it } from "vitest";

import {
  PERMISSION_PRESETS,
  currentPresetKey,
  isEmptyDiff,
  presetDiff,
  snapshotOf,
  type PermissionSnapshot,
} from "@/lib/permission-presets";

/** 功能：把某档位预设当成「当前快照」。 */
function snapshotOfPreset(key: "loose" | "standard" | "strict"): PermissionSnapshot {
  return snapshotOf(PERMISSION_PRESETS[key]);
}

/** 功能：旧版标准档快照（ask_tools 少 memory_assert，实际机器上就是这个状态）。 */
const STALE_STANDARD: PermissionSnapshot = {
  askRisk: ["high", "critical"],
  askTools: ["bash", "write", "ontology_define", "ontology_assert"],
  bypass: ["read", "web_search", "glob", "grep", "ontology_query"],
};

describe("currentPresetKey", () => {
  it("三项完全一致时能反推档位", () => {
    expect(currentPresetKey(snapshotOfPreset("loose"))).toBe("loose");
    expect(currentPresetKey(snapshotOfPreset("standard"))).toBe("standard");
    expect(currentPresetKey(snapshotOfPreset("strict"))).toBe("strict");
  });

  it("顺序不同不影响判定", () => {
    const reversed: PermissionSnapshot = {
      askRisk: [...snapshotOfPreset("standard").askRisk].reverse(),
      askTools: [...snapshotOfPreset("standard").askTools].reverse(),
      bypass: [...snapshotOfPreset("standard").bypass].reverse(),
    };
    expect(currentPresetKey(reversed)).toBe("standard");
  });

  it("旧版标准档快照（少一个 ask 工具）→ 判定为自定义，不冒充标准", () => {
    expect(currentPresetKey(STALE_STANDARD)).toBeNull();
  });

  it("破坏者：多一个免询问工具也必须判成自定义", () => {
    const extra: PermissionSnapshot = {
      ...snapshotOfPreset("loose"),
      bypass: [...snapshotOfPreset("loose").bypass, "todo_read"],
    };
    expect(currentPresetKey(extra)).toBeNull();
  });
});

describe("presetDiff", () => {
  it("旧快照切严格：多问 medium、5 个工具要确认、5 个不再免询问", () => {
    const diff = presetDiff("strict", STALE_STANDARD);
    expect(diff.riskAdd).toEqual(["medium"]);
    expect(diff.riskDel).toEqual([]);
    expect(diff.toolAdd).toEqual(["memory_assert", "read", "web_search", "glob", "grep"]);
    expect(diff.bypassDel).toHaveLength(5);
    expect(diff.bypassAdd).toEqual([]);
  });

  it("旧快照切标准：只差 memory_assert 一个工具", () => {
    const diff = presetDiff("standard", STALE_STANDARD);
    expect(diff.toolAdd).toEqual(["memory_assert"]);
    expect(diff.toolDel).toEqual([]);
    expect(diff.riskAdd).toEqual([]);
    expect(diff.bypassAdd).toEqual([]);
  });

  it("旧快照切宽松：少问 high、少问两个工具、多两个免询问", () => {
    const diff = presetDiff("loose", STALE_STANDARD);
    expect(diff.riskDel).toEqual(["high"]);
    expect(diff.toolDel).toEqual(["ontology_define", "ontology_assert"]);
    expect(diff.bypassAdd).toEqual(["todo_read", "todo_write"]);
  });

  it("与当前档位完全一致时差异为空", () => {
    expect(isEmptyDiff(presetDiff("standard", snapshotOfPreset("standard")))).toBe(true);
    expect(isEmptyDiff(presetDiff("strict", STALE_STANDARD))).toBe(false);
  });
});
