/**
 * 功能：可插槽容器注册表。
 * 壳层只认固定 region；新面板（含 插件贡献）往注册表挂，AppShell 零改动。
 */
import type { ComponentType } from "react";

import type { SlotRegion } from "@/types";

export interface SlotRegistration {
  /** 挂载区域。 */
  region: SlotRegion;
  /** 同区域内唯一 id（重复注册后者覆盖）。 */
  id: string;
  /** 面板组件。 */
  component: ComponentType;
  /** 排序权重，越小越靠前。 */
  order?: number;
}

const registry = new Map<SlotRegion, Map<string, SlotRegistration>>();

/** 功能：注册一个槽位面板，返回注销函数。 */
export function registerSlot(registration: SlotRegistration): () => void {
  let bucket = registry.get(registration.region);
  if (!bucket) {
    bucket = new Map();
    registry.set(registration.region, bucket);
  }
  bucket.set(registration.id, registration);
  return () => bucket?.delete(registration.id);
}

/** 功能：取某区域已注册面板（按 order 升序）。 */
export function getSlots(region: SlotRegion): SlotRegistration[] {
  const bucket = registry.get(region);
  if (!bucket) return [];
  return [...bucket.values()].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
}

/** 功能：清空注册表（测试用）。 */
export function clearSlots(): void {
  registry.clear();
}
