/**
 * 功能：SessionTools 行为测试（无边框，搜索在前、历史在后；
 * 搜索 icon 点击后向左展开为搜索框）。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SessionTools } from "@/components/chat/SessionTools";

const ITEMS = [
  { id: "m1", role: "user", content: "帮我核对价格带" },
  { id: "m2", role: "assistant", content: "已收到，正在处理" },
];

describe("SessionTools", () => {
  it("图标顺序：搜索在前、历史在后（未展开时只有两个图标）", () => {
    render(<SessionTools items={ITEMS} testId="t" />);

    const root = screen.getByTestId("t");
    expect(root.children).toHaveLength(2);
    expect(root.children[0]?.getAttribute("data-testid")).toBe("t-search");
    expect(root.children[1]?.getAttribute("data-testid")).toBe("t-history");
  });

  it("搜索 icon 点击后向左展开为搜索框，输入关键词过滤出消息", async () => {
    const user = userEvent.setup();
    render(<SessionTools items={ITEMS} testId="t" />);

    expect(screen.queryByTestId("t-search-box")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("t-search"));
    expect(await screen.findByTestId("t-search-box")).toBeInTheDocument();
    // 未输入时不出结果浮层
    expect(screen.queryByTestId("t-search-results")).not.toBeInTheDocument();

    await user.type(screen.getByTestId("t-search-input"), "核对");
    const hits = await screen.findAllByTestId("t-search-item");
    expect(hits).toHaveLength(1);
    expect(hits[0]).toHaveTextContent("帮我核对价格带");

    await user.click(screen.getByTestId("t-search-close"));
    expect(screen.queryByTestId("t-search-box")).not.toBeInTheDocument();
  });

  it("工具按钮与顶栏收缩按钮同尺寸/同色值（size-8 + muted/accent 色板——2026-09-24 真机反馈）", () => {
    // jsdom 无布局——钉与 SidebarTrigger 一致的尺寸与色值类（防回归为 size-6 无色值）
    render(<SessionTools items={ITEMS} testId="t" />);
    for (const id of ["t-search", "t-history"]) {
      const btn = screen.getByTestId(id);
      expect(btn.className).toContain("size-8");
      expect(btn.className).toContain("text-muted-foreground");
      expect(btn.className).toContain("hover:bg-primary/5");
      expect(btn.className).toContain("hover:text-accent-foreground");
    }
  });
});
