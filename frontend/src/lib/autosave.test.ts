import { describe, expect, it } from "vitest";

import { LARGE_SAVE_BYTES, autosaveDelay } from "@/lib/autosave";

describe("autosaveDelay", () => {
  it("普通文本 600ms 防抖", () => {
    expect(autosaveDelay(16)).toBe(600);
  });

  it("超过 10MB 的文本降频到 5s", () => {
    const boundary = Math.floor(LARGE_SAVE_BYTES / 3);
    expect(autosaveDelay(boundary + 1)).toBe(5000);
  });

  it("恰好 10MB 以内仍用 600ms", () => {
    const boundary = Math.floor(LARGE_SAVE_BYTES / 3);
    expect(autosaveDelay(boundary)).toBe(600);
  });
});
