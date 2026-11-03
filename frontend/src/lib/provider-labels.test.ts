/**
 * 功能：供应商标签契约（二次核实批：单名不长备注 + 新增 MiniMax/MiMo/Ollama）。
 */
import { describe, expect, it } from "vitest";

import { OFFICIAL_PROVIDERS, providerLabel } from "@/lib/provider-labels";

describe("provider-labels", () => {
  it("官方清单覆盖新增供应商（minimax/mimo/ollama）", () => {
    expect(OFFICIAL_PROVIDERS).toContain("minimax");
    expect(OFFICIAL_PROVIDERS).toContain("mimo");
    expect(OFFICIAL_PROVIDERS).toContain("ollama");
  });

  it("OFFICIAL_PROVIDERS 按字母序", () => {
    const sorted = [...OFFICIAL_PROVIDERS].sort((a, b) => a.localeCompare(b));
    expect(OFFICIAL_PROVIDERS).toEqual(sorted);
  });

  it("单名展示：不做中英并列备注（有官方中文名用中文）", () => {
    expect(providerLabel("deepseek")).toBe("深度求索");
    expect(providerLabel("moonshot")).toBe("月之暗面");
    expect(providerLabel("mimo")).toBe("小米 MiMo");
    expect(providerLabel("minimax")).toBe("MiniMax");
    expect(providerLabel("ollama")).toBe("Ollama");
  });

  it("未知 provider 原样回落（自定义标识不丢信息）", () => {
    expect(providerLabel("openrouter")).toBe("openrouter");
  });
});
