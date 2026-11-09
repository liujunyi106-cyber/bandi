import { describe, expect, it } from "vitest";

import { applyToolResult, type ChatMessage, type CitationRef } from "@/stores/chat-store";

const CITATIONS: CitationRef[] = [
  {
    id: "kref_1",
    label: "[1]",
    title: "产品手册",
    source_path: "docs/manual.md",
    chunk_id: "chunk-1",
  },
];

function message(): ChatMessage {
  return {
    id: "m1",
    sessionId: "s1",
    role: "assistant",
    content: "",
    done: false,
    process: [
      {
        id: "n1",
        kind: "tool",
        toolName: "knowledge_search",
        status: "running",
        toolCallId: "call-1",
      },
    ],
  };
}

describe("applyToolResult citations", () => {
  it("把 tool_result.citations 落到过程节点（回答 chips 数据源）", () => {
    const [updated] = applyToolResult([message()], "m1", {
      toolName: "knowledge_search",
      success: true,
      preview: "命中 1 条",
      durationMs: 12,
      toolCallId: "call-1",
      citations: CITATIONS,
    });

    const node = updated.process?.[0];
    expect(node?.kind).toBe("tool");
    if (node?.kind === "tool") {
      expect(node.citations).toEqual(CITATIONS);
    }
  });

  it("无 citations 时写空数组（不继承其他节点的旧引用）", () => {
    const [updated] = applyToolResult([message()], "m1", {
      toolName: "knowledge_search",
      success: true,
      preview: "b",
      durationMs: 1,
      toolCallId: "call-1",
    });
    const toolNode = updated.process?.[0];
    if (toolNode?.kind === "tool") {
      expect(toolNode.citations).toEqual([]);
    }
  });
});
