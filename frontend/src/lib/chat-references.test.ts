/**
 * 功能：chat-references 单测——引用文件提取（read/grep/glob 路径、去重
 * 上限、忽略非法输入）。
 */
import { describe, expect, it } from "vitest";

import { extractReferencedFiles } from "@/lib/chat-references";
import type { ProcessToolNode } from "@/stores/chat-store";

function node(toolName: string, input?: Record<string, unknown>): ProcessToolNode {
  return { id: toolName, kind: "tool", toolName, input, status: "success" };
}

describe("extractReferencedFiles", () => {
  it("提取 read/grep/glob 的路径并去重保序", () => {
    const out = extractReferencedFiles([
      node("read", { file_path: "a.md" }),
      node("grep", { pattern: "foo" }),
      node("read", { file_path: "a.md" }),
      node("bash", { command: "ls" }),
    ]);
    expect(out).toEqual(["a.md", "foo"]);
  });

  it("忽略空/非字符串路径与无 process", () => {
    expect(extractReferencedFiles(undefined)).toEqual([]);
    expect(extractReferencedFiles([node("read", { file_path: "  " })])).toEqual([]);
    expect(extractReferencedFiles([node("read", { file_path: 42 })])).toEqual([]);
  });

  it("上限 10 条", () => {
    const nodes = Array.from({ length: 15 }, (_, i) => node("read", { file_path: `f${i}.md` }));
    expect(extractReferencedFiles(nodes)).toHaveLength(10);
  });
});
