import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NodeBody } from "@/pages/knowledge/NodeBody";
import type { KnowledgeNode } from "@/services/knowledge-nodes";

const LINK: KnowledgeNode = {
  id: "l1",
  base_id: "b1",
  parent_id: "",
  kind: "link",
  name: "官网",
  sort_order: 1,
  employee_id: null,
  size: 0,
  url: "https://example.com",
  updated_at: 1,
};

describe("NodeBody link snapshot", () => {
  it("有快照时渲染清洗后的 iframe 与重新抓取入口", () => {
    render(
      <NodeBody
        content={{
          kind: "link",
          url: "https://example.com",
          snapshot_html: "<article><h1>快照正文</h1></article>",
          snapshot_at: 1_700_000_000,
        }}
        items={[]}
        node={LINK}
        onOpenChild={vi.fn()}
        onSnapshot={vi.fn()}
      />,
    );
    expect(screen.getByText("重新抓取")).toBeInTheDocument();
    expect(screen.getByTitle("官网").getAttribute("srcdoc")).toContain("快照正文");
  });

  it("无快照时点击抓取正文触发回调", () => {
    const onSnapshot = vi.fn();
    render(
      <NodeBody
        content={{ kind: "link", url: "https://example.com" }}
        items={[]}
        node={LINK}
        onOpenChild={vi.fn()}
        onSnapshot={onSnapshot}
      />,
    );
    fireEvent.click(screen.getByText("抓取正文"));
    expect(onSnapshot).toHaveBeenCalledTimes(1);
  });
});
