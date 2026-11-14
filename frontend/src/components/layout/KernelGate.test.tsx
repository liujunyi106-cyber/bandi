import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { KernelGate } from "@/components/layout/KernelGate";

const CHILD = "已连上内核的业务界面";

/** 功能：按给定协议与查询串伪造 location，模拟 http 正常壳与 file 降级壳。 */
function stubLocation(protocol: string, search: string): void {
  vi.stubGlobal("location", { protocol, search, href: `${protocol}//x/${search}` });
}

describe("KernelGate 内核就绪门", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("file 降级壳在 boot=failed 时不渲染业务界面（不得假装内核可用）", () => {
    stubLocation("file:", "?boot=failed");
    render(<KernelGate>{CHILD}</KernelGate>);

    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
    expect(screen.getByText("本地内核未能启动，界面功能不可用")).toBeInTheDocument();
    expect(screen.getByText("重试")).toBeInTheDocument();
  });

  it("file 降级壳无 boot 参数时同样按不可用处理", () => {
    stubLocation("file:", "");
    render(<KernelGate>{CHILD}</KernelGate>);

    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
    expect(screen.getByText("本地内核未能启动，界面功能不可用")).toBeInTheDocument();
  });

  it("file 降级壳 boot=starting 时显示启动中而非业务界面", () => {
    stubLocation("file:", "?boot=starting");
    render(<KernelGate>{CHILD}</KernelGate>);

    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
    expect(screen.getByText("正在启动本地内核…")).toBeInTheDocument();
    // 启动中不提供重试按钮（内核仍在拉起）
    expect(screen.queryByText("重试")).not.toBeInTheDocument();
  });

  it("/health 就绪后渲染业务界面", async () => {
    stubLocation("http:", "");
    // 测试环境默认短路成 ready，这里临时把 MODE 改回非 test 以验证真实轮询
    vi.stubEnv("MODE", "production");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "application/json" },
      }),
    );

    render(<KernelGate>{CHILD}</KernelGate>);

    expect(await screen.findByText(CHILD)).toBeInTheDocument();
  });

  it("/health 返回非 JSON（被 SPA 兜底路由接管）时不判定就绪", async () => {
    stubLocation("http:", "");
    vi.stubEnv("MODE", "production");
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        headers: { get: () => "text/html" },
      }),
    );

    render(<KernelGate>{CHILD}</KernelGate>);

    expect(await screen.findByText("正在启动本地内核…")).toBeInTheDocument();
    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
  });
});
