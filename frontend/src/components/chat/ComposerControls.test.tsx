/**
 * 功能：控制条思考挡位归一测试（第九轮）——
 * 档案模型带网关前缀（xiaomi/mimo-v2.6-flash）时必须归一匹配目录键（mimo-v2.6-flash）
 * 否则挡位恒禁用（此前的真实缺陷）。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ComposerControls } from "@/components/chat/ComposerControls";
import type { ModelOption } from "@/services/settings";

vi.mock("@/components/chat/ModelConfigDialog", () => ({
  ModelConfigDialog: () => null,
}));

const MIMO: ModelOption = {
  name: "mimo-v2.6-flash",
  provider: "mimo",
  supports_reasoning: true,
  reasoning_efforts: ["low", "high"],
};

function renderControls(model: string, options: ModelOption[]) {
  return render(
    <ComposerControls
      model={model}
      options={options}
      thinking=""
      activeProfile={null}
      onActivateProfile={vi.fn()}
      onThinking={vi.fn()}
      onProfilesChanged={vi.fn()}
    />,
  );
}

describe("ComposerControls 思考挡位归一（第九轮）", () => {
  it("档案模型带前缀（xiaomi/mimo-v2.6-flash）→ 归一命中目录键，挡位可用", () => {
    renderControls("xiaomi/mimo-v2.6-flash", [MIMO]);
    expect(screen.getByTestId("composer-thinking")).not.toBeDisabled();
  });

  it("破坏者：目录无可匹配条目 → 挡位禁用（自定义模型且无能力数据）", () => {
    renderControls("custom-gw/unknown-model", [MIMO]);
    expect(screen.getByTestId("composer-thinking")).toBeDisabled();
  });
});
