/**
 * 功能：模型候选合成契约——已配置档案模型必须出现在控制条候选里。
 *
 * 钉行为：① 目录外档案模型被并入 ② 与目录同模型（归一等价）不重复 ③ 不虚报能力
 * ④ 空档案/空模型不产生垃圾条目
 */
import { describe, expect, it } from "vitest";

import { configuredModelOptions, mergeConfiguredProfiles } from "@/lib/model-options";
import type { ModelOption } from "@/services/settings";

const dir: ModelOption[] = [
  {
    name: "mimo-v2.6-flash",
    provider: "openrouter",
    supports_reasoning: true,
    reasoning_efforts: ["low", "high"],
  },
];

describe("mergeConfiguredProfiles", () => {
  it("并入目录外的档案模型（网关私有模型）", () => {
    const merged = mergeConfiguredProfiles(dir, [{ model: "custom-hub-flash", provider: "custom_hub" }]);
    expect(merged.map((item) => item.name)).toContain("custom-hub-flash");
    expect(merged.find((item) => item.name === "custom-hub-flash")?.provider).toBe("custom_hub");
  });

  it("与目录等价（归一后同模型）不重复追加", () => {
    const merged = mergeConfiguredProfiles(dir, [
      { model: "xiaomi/mimo-v2.6-flash", provider: "openrouter" },
    ]);
    expect(merged).toHaveLength(1);
  });

  it("不虚报思考能力（目录外模型默认无挡位）", () => {
    const merged = mergeConfiguredProfiles(dir, [{ model: "custom-hub-flash", provider: "custom_hub" }]);
    const extra = merged.find((item) => item.name === "custom-hub-flash");
    expect(extra?.supports_reasoning).toBe(false);
    expect(extra?.reasoning_efforts).toEqual([]);
  });

  it("多个档案全部并入且顺序稳定（目录在前）", () => {
    const merged = mergeConfiguredProfiles(dir, [
      { model: "custom-hub-flash", provider: "custom_hub" },
      { model: "deepseek-v4.1-flash", provider: "deepseek" },
    ]);
    expect(merged.map((item) => item.name)).toEqual([
      "mimo-v2.6-flash",
      "custom-hub-flash",
      "deepseek-v4.1-flash",
    ]);
  });

  it("空模型名的档案被忽略（不产生垃圾条目）", () => {
    const merged = mergeConfiguredProfiles(dir, [{ model: "  ", provider: "x" }]);
    expect(merged).toHaveLength(1);
  });

  it("无档案时原样返回（不复制数组语义破坏）", () => {
    const merged = mergeConfiguredProfiles(dir, []);
    expect(merged).toBe(dir);
  });
});

describe("configuredModelOptions", () => {
  it("只保留已配置档案模型，过滤目录中未配置项", () => {
    const result = configuredModelOptions(
      [
        ...dir,
        {
          name: "deepseek-v4.1-flash",
          provider: "deepseek",
          supports_reasoning: true,
          reasoning_efforts: ["high"],
        },
      ],
      [{ model: "xiaomi/mimo-v2.6-flash", provider: "openrouter" }],
      "",
      false,
    );
    expect(result.map((item) => item.name)).toEqual(["mimo-v2.6-flash"]);
    expect(result[0].reasoning_efforts).toEqual(["low", "high"]);
  });

  it("全局模型有凭证时保留为跟随项", () => {
    const result = configuredModelOptions(dir, [], "mimo-v2.6-flash", true);
    expect(result.map((item) => item.name)).toEqual(["mimo-v2.6-flash"]);
  });

  it("目录外档案模型保留但不虚报思考能力", () => {
    const result = configuredModelOptions(dir, [{ model: "custom-hub-flash", provider: "custom_hub" }], "", false);
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("custom-hub-flash");
    expect(result[0].supports_reasoning).toBe(false);
  });
});
