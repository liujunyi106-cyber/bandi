import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { CitationChips } from "@/components/chat/CitationChips";
import type { CitationRef } from "@/stores/chat-store";

function citation(index: number, overrides: Partial<CitationRef> = {}): CitationRef {
  return {
    id: `kref_${index}`,
    label: `[${index}]`,
    title: `文档 ${index}`,
    source_path: `docs/doc-${index}.md`,
    chunk_id: `chunk-${index}`,
    excerpt: `引用原文 ${index}`,
    ...overrides,
  };
}

describe("CitationChips", () => {
  it("去重并最多展示 4 条；点击展开引用原文", async () => {
    const user = userEvent.setup();
    render(
      <CitationChips
        citations={[
          citation(1),
          citation(1),
          citation(2),
          citation(3),
          citation(4),
          citation(5, { id: "kref_5", source_path: "docs/doc-5.md" }),
        ]}
      />,
    );

    const chips = screen.getAllByTestId("citation-chip");
    expect(chips).toHaveLength(4);
    expect(screen.getByText("文档 1")).toBeInTheDocument();

    await user.click(chips[0]);
    const detail = screen.getByTestId("citation-detail");
    expect(detail).toHaveTextContent("引用原文 1");
    expect(detail).toHaveTextContent("docs/doc-1.md");

    await user.click(screen.getByText("关闭"));
    expect(screen.queryByTestId("citation-detail")).not.toBeInTheDocument();
  });

  it("无引用不渲染空壳", () => {
    const { container } = render(<CitationChips citations={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
