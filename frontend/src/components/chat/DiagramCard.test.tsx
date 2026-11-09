/**
 * 功能：DiagramCard 组件契约——图表卡片「…」菜单四功能、查看代码↔显示 UI
 * 切换（菜单项与图标随之变化）、复制代码。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async () => ({ svg: "<svg data-testid='fake-svg'></svg>" })),
  },
}));

import { DiagramCard } from "@/components/chat/DiagramCard";

const CODE = "graph TD; A-->B;";

describe("DiagramCard", () => {
  it("菜单四功能齐全（查看代码/复制代码/下载到本地/下载为图片）", async () => {
    const user = userEvent.setup();
    render(<DiagramCard code={CODE} />);
    await user.click(screen.getByTestId("diagram-more"));
    expect(await screen.findByText("查看代码")).toBeInTheDocument();
    expect(screen.getByText("复制代码")).toBeInTheDocument();
    expect(screen.getByText("下载到本地")).toBeInTheDocument();
    expect(screen.getByText("下载为图片")).toBeInTheDocument();
  });

  it("渲染成功后显示 UI；「查看代码」切换为代码且菜单项变「显示 UI」", async () => {
    const user = userEvent.setup();
    render(<DiagramCard code={CODE} />);
    await waitFor(() => expect(screen.getByTestId("diagram-svg")).toBeInTheDocument());
    await user.click(screen.getByTestId("diagram-more"));
    await user.click(screen.getByTestId("diagram-toggle-view"));
    expect(screen.getByTestId("diagram-code")).toHaveTextContent("graph TD");
    await user.click(screen.getByTestId("diagram-more"));
    expect(await screen.findByText("显示 UI")).toBeInTheDocument();
  });

  it("复制代码写入剪贴板", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(<DiagramCard code={CODE} />);
    await user.click(screen.getByTestId("diagram-more"));
    await user.click(screen.getByTestId("diagram-copy"));
    expect(writeText).toHaveBeenCalledWith(CODE);
  });
});
