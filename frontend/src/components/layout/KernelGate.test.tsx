// 功能：契约——内核就绪门只做门禁，启动动画属于独立文档 boot.html。
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KernelGate } from "@/components/layout/KernelGate";

const CHILD = "业务界面";

/** 功能：模拟壳注入的启动上下文。 */
function stubLocation(protocol: string, search: string): void {
  vi.stubGlobal("location", { ...window.location, protocol, search });
}

describe("KernelGate 内核就绪门", () => {
  beforeEach(() => {
    vi.stubEnv("MODE", "test");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("测试环境直接放行业务界面（不探活）", () => {
    stubLocation("http:", "?boot=ready");
    render(<KernelGate>{CHILD}</KernelGate>);
    expect(screen.getByText(CHILD)).toBeInTheDocument();
  });

  it("boot=failed 时不得渲染业务界面（不能假装内核可用）", () => {
    stubLocation("file:", "?boot=failed");
    render(<KernelGate>{CHILD}</KernelGate>);
    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
    expect(screen.getByText("重试")).toBeInTheDocument();
  });

  it("等待期先渲染界面（不白屏），且不画进度条", () => {
    vi.stubEnv("MODE", "production");
    stubLocation("http:", "?boot=ready");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("not up")));
    render(<KernelGate>{CHILD}</KernelGate>);

    // 空白屏幕比少几个数据更糟：就绪前就先把界面交出去
    expect(screen.getByText(CHILD)).toBeInTheDocument();
    // 启动动画与进度条都在 boot.html：SPA 里再画一遍就是「两遍过场」
    expect(screen.queryByTestId("boot-progress")).not.toBeInTheDocument();
    expect(screen.queryByTestId("boot-animation")).not.toBeInTheDocument();
  });

  it("/health 就绪后渲染业务界面", async () => {
    vi.stubEnv("MODE", "production");
    stubLocation("http:", "?boot=ready");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "application/json" },
        json: async () => ({ status: "ok" }),
      }),
    );
    render(<KernelGate>{CHILD}</KernelGate>);
    await waitFor(() => expect(screen.getByText(CHILD)).toBeInTheDocument());
  });

  it("/health 返回非 JSON（被 SPA 兜底路由接管）时不判定就绪", async () => {
    vi.stubEnv("MODE", "production");
    stubLocation("http:", "?boot=ready");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "text/html" },
        json: async () => ({}),
      }),
    );
    render(<KernelGate>{CHILD}</KernelGate>);
    await new Promise((resolve) => setTimeout(resolve, 60));
    // 非 JSON 不算就绪：置灰遮罩仍在（界面已出，但交互被阻断）
    expect(screen.queryByTestId("kernel-gate")).toBeInTheDocument();
  });
});
