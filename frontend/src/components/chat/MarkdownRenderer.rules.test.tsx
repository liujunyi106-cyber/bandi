import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";

describe("MarkdownRenderer 空装饰块", () => {
  it("连续分割线不渲染两条白杠", () => {
    const { container } = render(
      <MarkdownRenderer text={"一、核心用户旅程\n\n---\n\n---\n\n首页"} />,
    );
    expect(container.querySelectorAll("hr")).toHaveLength(0);
    expect(container.textContent).toContain("核心用户旅程");
  });
});
