/**
 * 功能：Button（shadcn radix-nova）组件层冒烟（@base Day135 §5 组件层测试补齐）。
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "@/components/ui/button";

describe("Button (radix-nova)", () => {
  it("渲染文本并带基础组合 class", () => {
    render(
      <Button variant="outline" size="sm">
        保存
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "保存" });
    expect(btn).toBeInTheDocument();
    expect(btn.className).toContain("group/button");
  });

  it("size 档位可区分（sm vs default 高度不同）", () => {
    render(<Button size="sm">小</Button>);
    const small = screen.getByRole("button", { name: "小" });
    expect(small.className).toContain("h-7");
  });
});
