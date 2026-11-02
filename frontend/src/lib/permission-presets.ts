/**
 * 功能：权限档位预设与差异计算——前端需要它才能反推「当前属于哪一档」并预演切换代价。
 *
 * `PERMISSION_PRESETS` 必须与后端 `bandi/api/settings_ext.py` 的 `PRESETS` 逐项相等，
 * 由契约测试 `test_contract_permission_presets_source.py` 钉住（对象体是纯 JSON，便于比对）。
 */
import type { PermissionPreset } from "@/services/settings";

/** 功能：单个档位对「询问范围」的三项取值。 */
export interface PresetPolicy {
  ask_risk_levels: string[];
  ask_tools: string[];
  bypass_tools: string[];
}

/** 功能：当前生效的询问范围快照（三个集合，顺序无关）。 */
export interface PermissionSnapshot {
  askRisk: string[];
  askTools: string[];
  bypass: string[];
}

/** 功能：切换档位相对当前快照的增减项。 */
export interface PresetDiff {
  riskAdd: string[];
  riskDel: string[];
  toolAdd: string[];
  toolDel: string[];
  bypassAdd: string[];
  bypassDel: string[];
}

/** 功能：档位展示顺序（由宽到严）。 */
export const PRESET_ORDER: PermissionPreset[] = ["loose", "standard", "strict"];

export const PERMISSION_PRESETS: Record<PermissionPreset, PresetPolicy> = {
  "loose": {
    "ask_risk_levels": ["critical"],
    "ask_tools": ["bash", "write"],
    "bypass_tools": ["read", "web_search", "glob", "grep", "ontology_query", "todo_read", "todo_write"]
  },
  "standard": {
    "ask_risk_levels": ["high", "critical"],
    "ask_tools": ["bash", "write", "ontology_define", "ontology_assert", "memory_assert"],
    "bypass_tools": ["read", "web_search", "glob", "grep", "ontology_query"]
  },
  "strict": {
    "ask_risk_levels": ["medium", "high", "critical"],
    "ask_tools": ["bash", "write", "ontology_define", "ontology_assert", "memory_assert", "read", "web_search", "glob", "grep"],
    "bypass_tools": []
  }
};

/** 功能：把接口返回的权限配置整理成快照。 */
export function snapshotOf(permission: {
  ask_risk_levels: string[];
  ask_tools: string[];
  bypass_tools: string[];
}): PermissionSnapshot {
  return {
    askRisk: permission.ask_risk_levels,
    askTools: permission.ask_tools,
    bypass: permission.bypass_tools,
  };
}

/** 功能：集合相等判定（与顺序无关）。 */
function sameSet(left: string[], right: string[]): boolean {
  if (left.length !== right.length) return false;
  const rightSet = new Set(right);
  return left.every((item) => rightSet.has(item));
}

/** 功能：`source` 里有而 `other` 里没有的项。 */
function missingFrom(source: string[], other: string[]): string[] {
  const otherSet = new Set(other);
  return source.filter((item) => !otherSet.has(item));
}

/** 功能：当前快照命中哪一档；三项都不完全一致返回 null（= 自定义）。 */
export function currentPresetKey(snapshot: PermissionSnapshot): PermissionPreset | null {
  return (
    PRESET_ORDER.find((key) => {
      const policy = PERMISSION_PRESETS[key];
      return (
        sameSet(policy.ask_risk_levels, snapshot.askRisk) &&
        sameSet(policy.ask_tools, snapshot.askTools) &&
        sameSet(policy.bypass_tools, snapshot.bypass)
      );
    }) ?? null
  );
}

/** 功能：切到某档位相对当前快照的增减项（用于卡片上的差异行）。 */
export function presetDiff(preset: PermissionPreset, snapshot: PermissionSnapshot): PresetDiff {
  const policy = PERMISSION_PRESETS[preset];
  return {
    riskAdd: missingFrom(policy.ask_risk_levels, snapshot.askRisk),
    riskDel: missingFrom(snapshot.askRisk, policy.ask_risk_levels),
    toolAdd: missingFrom(policy.ask_tools, snapshot.askTools),
    toolDel: missingFrom(snapshot.askTools, policy.ask_tools),
    bypassAdd: missingFrom(policy.bypass_tools, snapshot.bypass),
    bypassDel: missingFrom(snapshot.bypass, policy.bypass_tools),
  };
}

/** 功能：差异是否为空（= 与当前一致）。 */
export function isEmptyDiff(diff: PresetDiff): boolean {
  return (
    diff.riskAdd.length === 0 &&
    diff.riskDel.length === 0 &&
    diff.toolAdd.length === 0 &&
    diff.toolDel.length === 0 &&
    diff.bypassAdd.length === 0 &&
    diff.bypassDel.length === 0
  );
}
