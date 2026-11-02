/**
 * 功能：右栏页签预览测试（文件/制品两源 / md 与行号 / 元数据卡 / 实时刷新）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PanelTabContent } from "@/components/chat/panel/FilePreview";
import { panelApi } from "@/services/panel";
import { useChatStore, type ChatMessage } from "@/stores/chat-store";

vi.mock("@/services/panel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/panel")>();
  return {
    ...actual,
    panelApi: {
      ...actual.panelApi,
      fileContent: vi.fn(async (_sessionId: string, path: string) => ({
        session_id: "s1",
        path,
        size: 12,
        kind: "text" as const,
        content: "line one\nline two",
        truncated: false,
      })),
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      artifactContent: vi.fn(async () => ({
        artifact_id: "a1",
        path: "docs/out.md",
        size: 6,
        truncated: false,
        kind: "text" as const,
        content: "artifact body",
      })),
    },
  };
});

describe("PanelTabContent", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ messagesBySession: {} });
  });

  it("文件文本预览：行号 + 内容", async () => {
    render(
      <PanelTabContent
        sessionId="s1"
        tab={{ id: "file:a.txt", kind: "file", title: "a.txt", path: "a.txt" }}
      />,
    );
    expect(await screen.findByText(/line one/)).toBeInTheDocument();
    // 行号列（两个 <pre> 的文本经 testing-library 空白归一：1\n2 → "1 2"）
    expect(screen.getByText("1 2")).toBeInTheDocument();
  });

  it("markdown 文件走渲染器（.md）", async () => {
    vi.mocked(panelApi.fileContent).mockResolvedValueOnce({
      session_id: "s1",
      path: "readme.md",
      size: 8,
      kind: "text",
      content: "# 标题",
      truncated: false,
    });
    render(
      <PanelTabContent
        sessionId="s1"
        tab={{ id: "file:readme.md", kind: "file", title: "readme.md", path: "readme.md" }}
      />,
    );
    expect(await screen.findByTestId("markdown-body")).toBeInTheDocument();
  });

  it("制品预览（137c-A2 端点复用）", async () => {
    render(
      <PanelTabContent
        sessionId="s1"
        tab={{
          id: "artifact:a1",
          kind: "artifact",
          title: "out.md",
          artifactId: "a1",
          path: "docs/out.md",
        }}
      />,
    );
    expect(await screen.findByText(/artifact body/)).toBeInTheDocument();
  });

  it("二进制 → 元数据卡（不回内容）", async () => {
    vi.mocked(panelApi.fileContent).mockResolvedValueOnce({
      session_id: "s1",
      path: "img.png",
      size: 2048,
      kind: "binary",
      content: "",
    });
    render(
      <PanelTabContent
        sessionId="s1"
        tab={{ id: "file:img.png", kind: "file", title: "img.png", path: "img.png" }}
      />,
    );
    expect(await screen.findByText(/二进制文件/)).toBeInTheDocument();
  });

  it("实时更新：write 成功 tool_result 帧匹配路径 → 自动刷新（第二次拉取）", async () => {
    vi.mocked(panelApi.fileContent).mockResolvedValueOnce({
      session_id: "s1",
      path: "a.txt",
      size: 2,
      kind: "text",
      content: "v1",
      truncated: false,
    });
    render(
      <PanelTabContent
        sessionId="s1"
        tab={{ id: "file:a.txt", kind: "file", title: "a.txt", path: "a.txt" }}
      />,
    );
    expect(await screen.findByText("v1")).toBeInTheDocument();

    vi.mocked(panelApi.fileContent).mockResolvedValueOnce({
      session_id: "s1",
      path: "a.txt",
      size: 2,
      kind: "text",
      content: "v2",
      truncated: false,
    });
    const messages: ChatMessage[] = [
      {
        id: "m1",
        sessionId: "s1",
        role: "assistant",
        content: "",
        done: true,
        process: [
          {
            id: "n1",
            kind: "tool",
            toolName: "write",
            input: { path: "a.txt" },
            status: "success",
            toolCallId: "call_9",
          },
        ],
      },
    ];
    useChatStore.setState({ messagesBySession: { s1: messages } });
    expect(await screen.findByText("v2")).toBeInTheDocument();
    await waitFor(() => expect(panelApi.fileContent).toHaveBeenCalledTimes(2));
  });
});
