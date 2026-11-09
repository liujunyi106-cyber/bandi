/**
 * 功能：自动保存节奏——普通文本 600ms 防抖；超大文本降频到 5s，避免每次全量写盘。
 */

export const SAVE_DEBOUNCE_MS = 600;
export const LARGE_SAVE_BYTES = 10 * 1024 * 1024;
export const LARGE_SAVE_DEBOUNCE_MS = 5000;

/** 功能：按字符数估算 UTF-8 字节（CJK 最多 3 字节），超过 10MB 用长防抖。 */
export function autosaveDelay(chars: number): number {
  return chars * 3 > LARGE_SAVE_BYTES ? LARGE_SAVE_DEBOUNCE_MS : SAVE_DEBOUNCE_MS;
}
