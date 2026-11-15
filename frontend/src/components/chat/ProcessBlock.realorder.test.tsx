/**
 * 功能：真机数据形态验证——用真实执行序列渲染 ProcessBlock，断言渲染行顺序。
 *
 * 数据取自真实工具调用形态（read → edit → bash → read），不是理想化样本。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ProcessBlock } from "@/components/chat/ProcessBlock";
import type { ProcessNode } from "@/stores/chat-store";

const REAL_SEQUENCE: ProcessNode[] = [
  {
    id: "ep:thought:1",
    kind: "thought",
    text: "**先确认现有实现**\n\n需要看 ProcessBlock 当前如何组织节点。",
    status: "success",
    durationMs: 2100,
  },
  {
    id: "ep:tool:2",
    kind: "tool",
    toolName: "read",
    input: { file_path: "frontend/src/components/chat/ProcessBlock.tsx" },
    status: "success",
    detail: "",
    durationMs: 43,
    toolCallId: "call_1",
  },
  {
    id: "ep:tool:3",
    kind: "tool",
    toolName: "grep",
    input: { pattern: "buildBuckets" },
    status: "success",
    detail: "",
    durationMs: 28,
    toolCallId: "call_2",
  },
  {
    id: "ep:skill:4",
    kind: "skill",
    skills: ["karpathy-guidelines"],
    count: 1,
    status: "success",
  },
  {
    id: "ep:tool:5",
    kind: "tool",
    toolName: "edit",
    input: { file_path: "frontend/src/components/chat/ProcessBlock.tsx" },
    status: "success",
    detail: "",
    durationMs: 31,
    toolCallId: "call_3",
  },
  {
    id: "ep:tool:6",
    kind: "tool",
    toolName: "bash",
    input: { command: "pnpm vitest run ProcessBlock" },
    status: "success",
    detail: "",
    durationMs: 194800,
    toolCallId: "call_4",
  },
  {
    id: "ep:tool:7",
    kind: "tool",
    toolName: "read",
    input: { file_path: "frontend/src/stores/chat-store.ts" },
    status: "error",
    detail: "",
    durationMs: 12,
    toolCallId: "call_5",
  },
];

describe("真机验证：执行流按真实顺序呈现", () => {
  it("渲染顺序 = 工具真实调用顺序，读/改/跑不按类别归并", () => {
    render(<ProcessBlock nodes={REAL_SEQUENCE} settled />);
    fireEvent.click(screen.getByTestId("process-toggle"));

    const rows = screen.getAllByTestId("process-timeline-row");
    const categories = rows.map((row) => row.getAttribute("data-category"));
    expect(categories).toEqual([
      "thought", // 先思考
      "explore", // 读 ProcessBlock.tsx
      "explore", // grep buildBuckets
      "skill", // 技能激活
      "file", // 改 ProcessBlock.tsx
      "tool", // 跑 vitest
      "explore", // 读 chat-store.ts（失败）
    ]);

    const texts = screen.getAllByTestId("process-item-text").map((n) => n.textContent ?? "");
    expect(texts[0]).toContain("先确认现有实现");
    expect(texts[1]).toContain("ProcessBlock.tsx");
    expect(texts[2]).toContain("buildBuckets");
    expect(texts[3]).toContain("karpathy-guidelines");
    expect(texts[4]).toContain("ProcessBlock.tsx");
    expect(texts[5]).toContain("vitest");
    expect(texts[6]).toContain("chat-store.ts");
  });

  it("失败行在原有位置呈现失败图标，不被挤到末尾", () => {
    render(<ProcessBlock nodes={REAL_SEQUENCE} settled />);
    fireEvent.click(screen.getByTestId("process-toggle"));

    const statuses = screen.getAllByTestId("process-item-status");
    // 只有最后一行（read chat-store.ts）是失败态
    const failed = statuses.filter((node) => node.querySelector("svg") !== null);
    expect(failed).toHaveLength(1);
    expect(statuses.indexOf(failed[0])).toBe(statuses.length - 1);
  });

  it("过程块折叠后行不可见，展开后全部可见", () => {
    render(<ProcessBlock nodes={REAL_SEQUENCE} settled />);
    expect(screen.queryByTestId("process-timeline-row")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getAllByTestId("process-timeline-row")).toHaveLength(7);
  });
});
