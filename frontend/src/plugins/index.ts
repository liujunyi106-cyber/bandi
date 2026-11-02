/**
 * 功能：插件宿主 契约（@base docs/前端设计规划.md §11 插件化演进口径 / docs/ADR.md ADR-38）。
 * 轨道落地；Day135 只固化「插件 manifest 形状 + iframe bridge 白名单」这两条边界
 * 让后续 前端插件（iframe sandbox + postMessage）不需要回头改壳。
 */
import type { SlotRegion } from "@/types";

/** 后端/前端插件清单（首启信任：schema 校验 + 校验和）。 */
export interface PluginManifest {
  id: string;
  name: string;
  version: string;
  /** 前端插件入口（dist 内相对路径）；后端能力插件留空。 */
  entry?: string;
  /** 该插件要挂载的槽位区域。 */
  slots?: SlotRegion[];
  /** 申请的能力（工具/Skill/LLM Adapter…），需用户确认。 */
  permissions?: string[];
}

/** iframe bridge 允许被插件调用的方法白名单（其余一律拒绝）。 */
export const BRIDGE_ALLOWED_METHODS = [
  "invokeRpc",
  "getTheme",
  "getLocale",
  "getVersions",
] as const;

export type BridgeMethod = (typeof BRIDGE_ALLOWED_METHODS)[number];

/** 功能：桥调用是否在白名单内。 */
export function isBridgeMethodAllowed(method: string): method is BridgeMethod {
  return (BRIDGE_ALLOWED_METHODS as readonly string[]).includes(method);
}
