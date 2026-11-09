/**
 * 功能：上下文用量环与面板测试——环形进度比例、面板五类百分比、无数据态。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { formatBucketPercent, formatRatioPercent, formatTokenK } from "@/lib/context-usage";

const contextUsage = vi.fn();

vi.mock("@/services/sessions", () => ({
  sessionsApi: { contextUsage: (sessionId: string) => contextUsage(sessionId) },
}));

const FULL = {
  session_id: "s1",
  available: true,
  model: "deepseek-v4.1-flash",
  context_window: 1000000,
  used_tokens: 250000,
  ratio: 0.25,
  buckets: [
    { key: "system", tokens: 100000, ratio: 0.1 },
    { key: "tools", tokens: 50000, ratio: 0.05 },
    { key: "messages", tokens: 80000, ratio: 0.08 },
    { key: "connectors", tokens: 10000, ratio: 0.01 },
    { key: "skills", tokens: 10000, ratio: 0.01 },
  ],
  updated_at: "2026-10-25T07:00:00",
};

const EMPTY = {
  session_id: "s1",
  available: false,
  model: "",
  context_window: 0,
  used_tokens: 0,
  ratio: 0,
  buckets: [],
  updated_at: "",
};

beforeEach(() => {
  contextUsage.mockReset();
});

describe("上下文用量环", () => {
  it("按 ratio 画环形进度，面板显示大号百分比与已使用/窗口", async () => {
    contextUsage.mockResolvedValue(FULL);
    render(<ContextUsageRing sessionId="s1" />);

    const ring = await screen.findByTestId("context-usage-ring");
    await waitFor(() => expect(ring).toHaveAttribute("data-ratio", "0.2500"));
    expect(screen.getByTestId("context-usage-ring-progress")).toBeInTheDocument();

    await userEvent.click(ring);
    expect(await screen.findByTestId("context-usage-panel")).toBeInTheDocument();
    expect(screen.getByText("上下文用量")).toBeInTheDocument();
    expect(screen.getByTestId("context-usage-percent")).toHaveTextContent("25.0%");
    expect(screen.getByTestId("context-usage-panel")).toHaveTextContent("已使用 250k / 1000k");

    // 右上角关闭键与标题同排（同一个 header 行），点击即收起面板
    const header = screen.getByTestId("context-usage-close").parentElement;
    expect(header).toContainElement(screen.getByText("上下文用量"));
    await userEvent.click(screen.getByTestId("context-usage-close"));
    await waitFor(() =>
      expect(screen.queryByTestId("context-usage-panel")).not.toBeInTheDocument(),
    );
  });

  it("五类各有图例行与进度条分段，百分比与后端一致", async () => {
    contextUsage.mockResolvedValue(FULL);
    render(<ContextUsageRing sessionId="s1" />);
    await userEvent.click(await screen.findByTestId("context-usage-ring"));

    expect(await screen.findByText("系统提示词")).toBeInTheDocument();
    expect(screen.getByText("工具和子智能体")).toBeInTheDocument();
    expect(screen.getByText("连接器及 MCP")).toBeInTheDocument();
    expect(screen.getByText("技能")).toBeInTheDocument();
    expect(screen.getByText("10.0%")).toBeInTheDocument();
    expect(screen.getByText("8.0%")).toBeInTheDocument();

    for (const key of ["system", "tools", "messages", "connectors", "skills"]) {
      expect(screen.getByTestId(`context-usage-bar-${key}`)).toBeInTheDocument();
    }
  });

  it("破坏者：无快照（available=false）→ 整个环不渲染，不留空圈", async () => {
    contextUsage.mockResolvedValue(EMPTY);
    render(<ContextUsageRing sessionId="s1" />);

    await waitFor(() => expect(contextUsage).toHaveBeenCalledWith("s1"));
    // 空圈会被读成「用量 0%」；真相是「还没测过」⇒ 不显示
    expect(screen.queryByTestId("context-usage-ring")).not.toBeInTheDocument();
    expect(screen.queryByTestId("context-usage-panel")).not.toBeInTheDocument();
  });

  it("破坏者：未选中会话（sessionId 空串）→ 不发请求且不渲染", () => {
    render(<ContextUsageRing sessionId="" />);
    expect(contextUsage).not.toHaveBeenCalled();
    expect(screen.queryByTestId("context-usage-ring")).not.toBeInTheDocument();
  });

  it("接口失败按无数据处理，不抛错且不渲染", async () => {
    contextUsage.mockRejectedValue(new Error("boom"));
    render(<ContextUsageRing sessionId="s1" />);
    await waitFor(() => expect(contextUsage).toHaveBeenCalled());
    expect(screen.queryByTestId("context-usage-ring")).not.toBeInTheDocument();
  });
});

describe("格式化口径", () => {
  it("token 折 k：1000 → 1.0k，123456 → 123k，1000000 → 1000k", () => {
    expect(formatTokenK(0)).toBe("0");
    expect(formatTokenK(999)).toBe("999");
    expect(formatTokenK(1000)).toBe("1.0k");
    expect(formatTokenK(123456)).toBe("123k");
    expect(formatTokenK(1000000)).toBe("1000k");
  });

  it("非零但小于 0.1% 显示 <0.1%，不再显示成 0.0%", () => {
    expect(formatBucketPercent(64, 0.000125)).toBe("<0.1%");
    expect(formatBucketPercent(0, 0)).toBe("0.0%");
  });

  it("百分比钳制在 [0,1]，越界不画出 >100%", () => {
    expect(formatRatioPercent(0.1234)).toBe("12.3%");
    expect(formatRatioPercent(1.5)).toBe("100.0%");
    expect(formatRatioPercent(-1)).toBe("0.0%");
  });
});
