/**
 * 功能：markdown 纯函数测试。
 */
import { describe, expect, it } from "vitest";

import { shouldUseDocumentLayout } from "@/lib/markdown";

describe("shouldUseDocumentLayout", () => {
  it("代码块 → 全宽文档布局", () => {
    expect(shouldUseDocumentLayout("看这段：\n```ts\nconst a = 1\n```")).toBe(true);
  });

  it("标题/列表/表格 → 全宽", () => {
    expect(shouldUseDocumentLayout("# 标题\n内容")).toBe(true);
    expect(shouldUseDocumentLayout("- 一\n- 二")).toBe(true);
    expect(shouldUseDocumentLayout("| A | B |\n|---|---|\n| 1 | 2 |")).toBe(true);
  });

  it("多段落（≥2）→ 全宽", () => {
    expect(shouldUseDocumentLayout("第一段。\n\n第二段。")).toBe(true);
  });

  it("短句回复 → 保持气泡栏（破坏者：普通一句话不得误判全宽）", () => {
    expect(shouldUseDocumentLayout("好的，收到～")).toBe(false);
    expect(shouldUseDocumentLayout("")).toBe(false);
  });
});
