/**
 * 功能：i18n 极简实现。
 * 不引 i18next——单页壳只需要「点路径查表 + 缺 key 回落」，复杂化无收益。
 * key 漂移由 `npm run i18n:check`（scripts/check-i18n.cjs）守门。
 */
import { useCallback } from "react";

import en from "./en.json";
import zh from "./zh.json";
import { useUiStore } from "@/stores/ui-store";

export const MESSAGES = { zh, en } as const;
export type Locale = keyof typeof MESSAGES;
export const LOCALES: Locale[] = ["zh", "en"];
export const DEFAULT_LOCALE: Locale = "zh";

type Tree = { [key: string]: string | Tree };

/** 功能：点路径取文案（`nav.chat` → "会话"）；缺失返回 key 本身，不抛错。 */
export function translate(locale: Locale, key: string): string {
  const walk = (tree: Tree, segments: string[]): string | undefined => {
    let node: string | Tree | undefined = tree;
    for (const segment of segments) {
      if (!node || typeof node === "string") return undefined;
      node = node[segment];
    }
    return typeof node === "string" ? node : undefined;
  };

  const segments = key.split(".");
  return (
    walk(MESSAGES[locale] as unknown as Tree, segments) ??
    walk(MESSAGES[DEFAULT_LOCALE] as unknown as Tree, segments) ??
    key
  );
}

/** 功能：组件内取文案函数（locale 跟随 ui store，切换即重渲染）。 */
export function useT(): (key: string) => string {
  const locale = useUiStore((state) => state.locale);
  return useCallback((key: string) => translate(locale, key), [locale]);
}
