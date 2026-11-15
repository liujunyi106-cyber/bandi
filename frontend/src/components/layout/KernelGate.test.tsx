import { render, screen, waitFor } from "@testing-library/react";
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
    expect(screen.getByText("启动失败，请重试")).toBeInTheDocument();
    expect(screen.getByText("重试")).toBeInTheDocument();
  });

  it("file 降级壳无 boot 参数时同样按不可用处理", () => {
    stubLocation("file:", "");
    render(<KernelGate>{CHILD}</KernelGate>);

    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
    expect(screen.getByText("启动失败，请重试")).toBeInTheDocument();
  });

  it("壳等待 sidecar 时只做静态等待，不画进度条（避免两次启动进度）", () => {
    stubLocation("file:", "?boot=waiting");
    render(<KernelGate>{CHILD}</KernelGate>);

    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
    expect(screen.getByTestId("boot-animation")).toBeInTheDocument();
    // 真实进度属于随后加载的 SPA；占位页再画一条会让用户连着看两次启动动画
    expect(screen.queryByTestId("boot-progress")).not.toBeInTheDocument();
    expect(screen.queryByTestId("boot-caption")).not.toBeInTheDocument();
    // 删除项：不得再出现“内核”与“已等待 Ns”
    expect(screen.queryByText(/内核/)).not.toBeInTheDocument();
    expect(screen.queryByText(/已等待/)).not.toBeInTheDocument();
    // 启动中不提供重试按钮（仍在拉起）
    expect(screen.queryByText("重试")).not.toBeInTheDocument();
  });

  it("历史 boot=starting 参数按静态等待处理（向后兼容旧壳）", () => {
    stubLocation("file:", "?boot=starting");
    render(<KernelGate>{CHILD}</KernelGate>);

    expect(screen.getByTestId("boot-animation")).toBeInTheDocument();
    expect(screen.queryByTestId("boot-progress")).not.toBeInTheDocument();
  });

  it("主进程上报的每个阶段都推进进度且不越界（http 壳内 SPA 才是唯一进度条）", async () => {
    stubLocation("http:", "");
    const bridge: { emit: ((stage: string) => void) | null } = { emit: null };
    vi.stubGlobal("bandi", {
      onBootStage: (cb: (stage: string) => void) => {
        bridge.emit = cb;
        return () => {
          bridge.emit = null;
        };
      },
    });

    render(<KernelGate>{CHILD}</KernelGate>);
    const bar = screen.getByTestId("boot-progress");
    const seen: number[] = [];

    for (const stage of ["spawn", "listening", "probing"]) {
      bridge.emit?.(stage);
      await waitFor(() => {
        const value = Number(bar.getAttribute("aria-valuenow"));
        expect(value).toBeGreaterThan(0);
      });
      seen.push(Number(bar.getAttribute("aria-valuenow")));
    }

    expect(seen).toEqual([...seen].sort((a, b) => a - b));
    expect(seen.every((v) => v < 100)).toBe(true);
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

    // 就绪后先播完成动画，再放行业务界面
    expect(await screen.findByText(CHILD, undefined, { timeout: 5000 })).toBeInTheDocument();
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

    expect(await screen.findByTestId("boot-progress")).toBeInTheDocument();
    expect(screen.queryByText(CHILD)).not.toBeInTheDocument();
  });
});
