/**
 * 功能：思考挡位滑杆测试（第九轮）——
 * models.dev 的 "none" = 关闭档语义：统一过滤（不与关闭档重复）；
 * 未知 effort（minimal/xhigh）label 原样显示；强制思考模型不出关闭档。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThinkingSlider } from "@/components/chat/ThinkingSlider";

function openSlider(): void {
  fireEvent.click(screen.getByTestId("composer-thinking"));
}

function sliderMax(): number {
  return Number((screen.getByTestId("thinking-slider") as HTMLInputElement).max);
}

describe("ThinkingSlider 档位处理（第九轮）", () => {
  it("过滤 models.dev 的 none 档（不与关闭档重复——7 档 → 关闭 + 6）", () => {
    render(
      <ThinkingSlider
        value=""
        efforts={["none", "minimal", "low", "medium", "high", "xhigh", "max"]}
        onChange={vi.fn()}
      />,
    );
    openSlider();
    // 关闭档 + 6 个 effort = 7 档 → max = 6（含 none 会是 7）
    expect(sliderMax()).toBe(6);
  });

  it("未知 effort（minimal/xhigh）仍可选且 label 原样（不丢档位）", () => {
    const onChange = vi.fn();
    render(
      <ThinkingSlider
        value=""
        efforts={["none", "minimal", "low", "medium", "high", "xhigh", "max"]}
        onChange={onChange}
      />,
    );
    openSlider();
    fireEvent.change(screen.getByTestId("thinking-slider"), { target: { value: "1" } });
    expect(onChange).toHaveBeenCalledWith("minimal");
  });

  it("强制思考模型（offSupported=false）：无关闭档且 none 一并过滤", () => {
    render(
      <ThinkingSlider
        value="low"
        efforts={["none", "low", "high"]}
        offSupported={false}
        onChange={vi.fn()}
      />,
    );
    openSlider();
    // 无关闭档 + 过滤 none → low/high 两档 → max = 1
    expect(sliderMax()).toBe(1);
  });
});
