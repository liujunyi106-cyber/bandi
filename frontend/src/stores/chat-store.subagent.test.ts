/**
 * 契约：子代理嵌套过程——applySubagentProgress 把子代理帧挂到
 * 对应 task 节点的 children；无匹配父节点时忽略（不挂错树）；content 不入树。
 */
import { describe, expect, it } from "vitest";

import { applySubagentProgress, type ChatMessage, type ProcessToolNode } from "./chat-store";

function _messages(): ChatMessage[] {
  return [
    {
      id: "p1",
      sessionId: "s1",
      role: "assistant",
      content: "",
      done: false,
      process: [
        { id: "t1", kind: "tool", toolName: "task", status: "running", toolCallId: "call_1" },
      ],
    } as ChatMessage,
  ];
}

const LABEL = { id: "subagent-x", parent_tool_call_id: "call_1" };

function _parent(list: ChatMessage[]): ProcessToolNode {
  return list[0].process?.find((node) => node.id === "t1") as ProcessToolNode;
}

describe("applySubagentProgress", () => {
  it("thinking/tool 帧挂到 task 节点 children", () => {
    let list = _messages();
    list = applySubagentProgress(list, "p1", LABEL, "thinking", { text: "先查资料。" });
    list = applySubagentProgress(list, "p1", LABEL, "tool_start", {
      tool_name: "bash",
      tool_call_id: "c9",
    });
    list = applySubagentProgress(list, "p1", LABEL, "tool_result", {
      tool_name: "bash",
      success: true,
      output_preview: "ok",
      duration_ms: 5,
    });
    const parent = _parent(list);
    expect(parent.children?.map((child) => child.kind)).toEqual(["thought", "tool"]);
    const tool = parent.children?.[1];
    expect(tool && tool.kind === "tool" && tool.status).toBe("success");
    expect(tool && tool.kind === "tool" && tool.detail).toBe("ok");
  });

  it("无匹配父节点时忽略（不挂错树）", () => {
    const list = applySubagentProgress(
      _messages(),
      "p1",
      { ...LABEL, parent_tool_call_id: "no-such-call" },
      "thinking",
      { text: "x" },
    );
    expect(_parent(list).children ?? []).toEqual([]);
  });

  it("content 帧不入树（正文不逐字进子树）", () => {
    const list = applySubagentProgress(_messages(), "p1", LABEL, "content", { text: "正文" });
    expect(_parent(list).children ?? []).toEqual([]);
  });

  it("tool_result 按 tool_call_id 回填对应子节点", () => {
    let list = _messages();
    list = applySubagentProgress(list, "p1", LABEL, "tool_start", {
      tool_name: "bash",
      tool_call_id: "c1",
    });
    list = applySubagentProgress(list, "p1", LABEL, "tool_start", {
      tool_name: "bash",
      tool_call_id: "c2",
    });
    list = applySubagentProgress(list, "p1", LABEL, "tool_result", {
      tool_name: "bash",
      success: false,
      output_preview: "boom",
      duration_ms: 3,
      tool_call_id: "c1",
    });
    const children = _parent(list).children ?? [];
    const first = children[0];
    const second = children[1];
    expect(first.kind === "tool" && first.status).toBe("error");
    expect(second.kind === "tool" && second.status).toBe("running");
  });
});
