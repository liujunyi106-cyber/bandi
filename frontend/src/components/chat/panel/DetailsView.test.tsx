/**
 * 功能：右栏详情视图测试（工具节点联动 / 空态 / 幽灵清理）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { DetailsView } from "@/components/chat/panel/DetailsView";
import { useChatStore, type ChatMessage } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

const messages: ChatMessage[] = [
  {
    id: "m1",
    sessionId: "s1",
    role: "assistant",
    content: "done",
    done: true,
    process: [
      {
        id: "n1",
        kind: "tool",
        toolName: "bash",
        input: { command: "ls" },
        status: "error",
        detail: "boom",
        durationMs: 5,
        toolCallId: "call_1",
      },
    ],
  },
];

describe("DetailsView", () => {
  beforeEach(() => {
    useChatStore.setState({ messagesBySession: { s1: messages } });
    usePanelStore.setState({ open: true, view: "details", toolFocus: null });
  });

  it("G4：工具节点 → 工具名/状态/耗时/输入/输出", () => {
    usePanelStore.setState({ toolFocus: { messageId: "m1", nodeId: "n1" } });
    render(<DetailsView sessionId="s1" />);
    expect(screen.getByTestId("panel-details")).toBeInTheDocument();
    expect(screen.getByText("bash")).toBeInTheDocument();
    expect(screen.getByText("失败")).toBeInTheDocument();
    expect(screen.getByText("5ms")).toBeInTheDocument();
    expect(screen.getByText(/command/)).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("未选中：空态提示", () => {
    render(<DetailsView sessionId="s1" />);
    expect(screen.getByTestId("panel-details-empty")).toBeInTheDocument();
  });

  it("破坏者：节点不可达（切会话/新轮清掉）→ 联动被清空", async () => {
    usePanelStore.setState({ toolFocus: { messageId: "m-ghost", nodeId: "n-ghost" } });
    render(<DetailsView sessionId="s1" />);
    await waitFor(() => expect(usePanelStore.getState().toolFocus).toBeNull());
  });
});
