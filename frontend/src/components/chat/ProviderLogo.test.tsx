/**
 * 功能：ProviderLogo 品牌图标契约（新增 minimax/mimo/ollama；
 * 未知 provider 回落首字母方块）。
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProviderLogo } from "@/components/chat/ProviderLogo";

describe("ProviderLogo", () => {
  it("新增三家品牌图标渲染 svg（非首字母方块）", () => {
    for (const provider of ["minimax", "mimo", "ollama"]) {
      const { container, unmount } = render(<ProviderLogo provider={provider} />);
      expect(container.querySelector("svg")).not.toBeNull();
      expect(container.textContent).toBe("");
      unmount();
    }
  });

  it("未知 provider 回落首字母方块", () => {
    const { container } = render(<ProviderLogo provider="openrouter" />);
    expect(container.querySelector("svg")).toBeNull();
    expect(container.textContent).toBe("O");
  });
});
