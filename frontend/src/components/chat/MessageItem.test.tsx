/**
 * 功能：MessageItem 组件行为测试（角色渲染 / 反馈回调 / 过程块）。
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MessageItem } from "@/components/chat/MessageItem";
import { usePanelStore } from "@/stores/panel-store";
import type { ChatMessage } from "@/stores/chat-store";

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    sessionId: "s1",
    role: "assistant",
    content: "回答内容",
    done: true,
    ...overrides,
  };
}

describe("MessageItem · 引用文件", () => {
  beforeEach(() => {
    usePanelStore.setState({ open: false, tabs: [], activeTabId: "" });
  });

  it("本地路径点击 → 在右栏打开文件页签（不再调系统打开）", async () => {
    const user = userEvent.setup();
    render(
      <MessageItem
        message={message({
          content: "找到了。",
          process: [
            {
              id: "n1",
              kind: "tool",
              toolName: "read",
              input: { file_path: "C:/work/README.md" },
              status: "success",
            },
          ],
        })}
      />,
    );
    const link = screen.getByTestId("referenced-file-link");
    expect(link).toHaveTextContent("C:/work/README.md");
    expect(link.className).toContain("text-primary");
    await user.click(link);
    const state = usePanelStore.getState();
    expect(state.open).toBe(true);
    expect(state.activeTabId).toBe("file:C:/work/README.md");
  });

  it("外部链接 → 新标签跳转，不进右栏", async () => {
    const user = userEvent.setup();
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    render(
      <MessageItem
        message={message({
          content: "参考资料见下。",
          process: [
            {
              id: "n1",
              kind: "tool",
              toolName: "read",
              input: { file_path: "https://example.com/doc" },
              status: "success",
            },
          ],
        })}
      />,
    );
    await user.click(screen.getByTestId("referenced-file-link"));
    expect(openSpy).toHaveBeenCalledWith(
      "https://example.com/doc",
      "_blank",
      "noopener,noreferrer",
    );
    expect(usePanelStore.getState().tabs).toHaveLength(0);
    openSpy.mockRestore();
  });

  it("引用文件渲染在回复气泡卡片内（不得落在卡片外）", () => {
    // L1 回归：引用文件一度渲染在气泡 div 外（卡片外侧），用户报障要求收回卡片内。
    render(
      <MessageItem
        message={message({
          content: "已读取该文件。",
          process: [
            {
              id: "n1",
              kind: "tool",
              toolName: "read",
              input: { file_path: "C:/work/README.md" },
              status: "success",
            },
          ],
        })}
      />,
    );
    const bubble = screen.getByTestId("message-bubble");
    expect(within(bubble).getByText("已读取该文件。")).toBeTruthy();
    const refs = within(bubble).getByTestId("referenced-files");
    expect(refs.parentElement).toBe(bubble);
    expect(within(refs).getByTestId("referenced-file-link")).toHaveTextContent("C:/work/README.md");
  });

  it("引用文件多条时分别可点", async () => {
    const user = userEvent.setup();
    render(
      <MessageItem
        message={message({
          process: [
            {
              id: "n1",
              kind: "tool",
              toolName: "read",
              input: { file_path: "a.md" },
              status: "success",
            },
            {
              id: "n2",
              kind: "tool",
              toolName: "grep",
              input: { path: "b/c.md" },
              status: "success",
            },
          ],
        })}
      />,
    );
    const links = screen.getAllByTestId("referenced-file-link");
    expect(links).toHaveLength(2);
    await user.click(links[1]);
    expect(usePanelStore.getState().activeTabId).toBe("file:b/c.md");
  });

  it("重复点击同一文件不产生重复页签（幂等）", async () => {
    const user = userEvent.setup();
    render(
      <MessageItem
        message={message({
          process: [
            {
              id: "n1",
              kind: "tool",
              toolName: "read",
              input: { file_path: "a.md" },
              status: "success",
            },
            {
              id: "n2",
              kind: "tool",
              toolName: "grep",
              input: { path: "a.md" },
              status: "success",
            },
          ],
        })}
      />,
    );
    await user.click(screen.getByTestId("referenced-file-link"));
    expect(usePanelStore.getState().tabs).toHaveLength(1);
  });
});

describe("MessageItem", () => {
  it("user 消息带角色标记并渲染内容", () => {
    render(<MessageItem message={message({ role: "user", content: "你好" })} interactive />);
    const item = screen.getByTestId("chat-message");
    expect(item.getAttribute("data-role")).toBe("user");
    expect(screen.getByText("你好")).toBeInTheDocument();
  });

  it('点赞回调：点一下即 onFeedback("up")（未赞态）', () => {
    const onFeedback = vi.fn();
    render(<MessageItem message={message()} interactive onFeedback={onFeedback} />);
    fireEvent.click(screen.getByTitle("回答有帮助"));
    expect(onFeedback).toHaveBeenCalledWith("up");
  });

  it("过程消息渲染工具分类与明细耗时（两级展开）", () => {
    render(
      <MessageItem
        message={message({
          content: "完成",
          process: [{ id: "t1", kind: "tool", toolName: "bash", status: "success", durationMs: 5 }],
        })}
        interactive
      />,
    );
    expect(screen.getByTestId("process-block")).toBeInTheDocument();
    // 完成后自动收起（延迟收起 timer 未到，初始为收起态）——展开总行 → 展开分类后可见明细
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    expect(screen.getByText("bash")).toBeInTheDocument();
    expect(screen.getAllByText("5ms")[0]).toBeInTheDocument();
  });

  it("system 消息渲居中提示样式", () => {
    render(<MessageItem message={message({ role: "system", content: "已发送转向指令" })} />);
    expect(screen.getByText("已发送转向指令")).toBeInTheDocument();
  });

  it("user 操作条精简：仅复制 + 编辑", () => {
    render(
      <MessageItem
        message={message({ role: "user", content: "你好", entryId: "e1" })}
        interactive
      />,
    );
    expect(screen.getByTitle("复制")).toBeInTheDocument();
    expect(screen.getByTestId("message-edit")).toBeInTheDocument();
    expect(screen.queryByTitle("重发")).not.toBeInTheDocument();
    expect(screen.queryByTitle("回答有帮助")).not.toBeInTheDocument();
    expect(screen.queryByTitle("回答有问题")).not.toBeInTheDocument();
    expect(screen.queryByTitle("更多操作")).not.toBeInTheDocument();
  });

  it("assistant 操作条顺序：复制、点赞、更多，最后时间戳（差评已下线）", () => {
    render(
      <MessageItem
        message={message({ entryId: "e9", createdAt: "2026-09-25T09:00:00" })}
        interactive
      />,
    );
    const actions = screen.getByTestId("message-actions");
    const buttons = Array.from(actions.querySelectorAll("button"));
    expect(buttons.map((b) => b.getAttribute("title"))).toEqual(["复制", "回答有帮助", "更多操作"]);
    expect(screen.queryByTitle("回答有问题"), "差评按钮已下线").not.toBeInTheDocument();
    const time = screen.getByTestId("message-time");
    const more = buttons[2]!;
    expect((more.compareDocumentPosition(time) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(
      true,
    );
  });

  it("点赞切换：已赞再点回落到 null（取消点赞）", async () => {
    const onFeedback = vi.fn().mockResolvedValue(undefined);
    render(
      <MessageItem
        message={message({ entryId: "e9", feedback: "up" })}
        interactive
        onFeedback={onFeedback}
      />,
    );
    await userEvent.click(screen.getByTitle("回答有帮助"));
    expect(onFeedback).toHaveBeenCalledWith(null);
  });

  it("user 操作条：时间戳紧贴编辑按钮（无撑开分隔），且右对齐", () => {
    render(
      <MessageItem
        message={message({
          role: "user",
          content: "你好",
          entryId: "e1",
          createdAt: "2026-09-25T09:00:00",
        })}
        interactive
      />,
    );
    const actions = screen.getByTestId("message-actions");
    expect(actions.querySelector(".flex-1")).toBeNull();
    // 右对齐：操作条不占满宽（外层 items-end 使其右贴气泡）
    expect(actions.className).not.toContain("w-full");
  });

  it("编辑消息：进入编辑态（提示 + 取消/发送）并回传新文本", () => {
    const onEditResend = vi.fn();
    render(
      <MessageItem
        message={message({ role: "user", content: "旧文本", entryId: "e1" })}
        interactive
        onEditResend={onEditResend}
      />,
    );
    fireEvent.click(screen.getByTestId("message-edit"));
    expect(screen.getByTestId("message-editor")).toBeInTheDocument();
    expect(screen.getByText("编辑后将从此处重新开始对话，已有产物不会被删除")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("message-edit-input"), { target: { value: "新文本" } });
    fireEvent.click(screen.getByTestId("message-edit-send"));
    expect(onEditResend).toHaveBeenCalledWith("m1", "新文本");
  });

  it("编辑取消：退出编辑态且不回调", () => {
    const onEditResend = vi.fn();
    render(
      <MessageItem
        message={message({ role: "user", content: "旧文本", entryId: "e1" })}
        interactive
        onEditResend={onEditResend}
      />,
    );
    fireEvent.click(screen.getByTestId("message-edit"));
    fireEvent.click(screen.getByTestId("message-edit-cancel"));
    expect(screen.queryByTestId("message-editor")).not.toBeInTheDocument();
    expect(onEditResend).not.toHaveBeenCalled();
  });

  it("⋯ 菜单按后端 entry 定位：本地待定稿消息不显示（防无效 revert）", () => {
    render(<MessageItem message={message({ role: "user", local: true })} interactive />);
    expect(screen.queryByTitle("更多操作")).not.toBeInTheDocument();
  });

  it("有 entryId 时 ⋯ 菜单传 entryId（不是本地临时 id）", async () => {
    const onRevertStage = vi.fn();
    // Radix 菜单经 portal 挂载 + 打开时给 body 加 pointer-events:none → 关掉指针校验
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(
      <MessageItem
        message={message({ id: "local-key", entryId: "e9" })}
        interactive
        onRevertStage={onRevertStage}
      />,
    );
    await user.click(screen.getByTitle("更多操作"));
    await user.click(await screen.findByText("回滚到此消息"));
    expect(onRevertStage).toHaveBeenCalledWith("e9");
  });

  it("回复块时间戳：HH:mm 常显 + hover 精确时间", () => {
    render(<MessageItem message={message({ createdAt: "2026-09-23T14:30:00" })} interactive />);
    const time = screen.getByTestId("message-time");
    expect(time).toHaveTextContent("14:30");
    expect(time.getAttribute("title")).toBe("2026-09-23 14:30:00");
  });

  it("无 createdAt 时不渲染时间戳（本地/旧消息防御）", () => {
    render(<MessageItem message={message()} interactive />);
    expect(screen.queryByTestId("message-time")).not.toBeInTheDocument();
  });

  it("空流式期：「正在回复中（模型调用中）」状态行（无空白气泡、无光标）", () => {
    render(<MessageItem message={message({ content: "", done: false })} interactive />);
    expect(screen.getByTestId("streaming-status")).toHaveTextContent("正在回复中…");
    expect(screen.queryByTestId("markdown-body")).not.toBeInTheDocument();
    expect(screen.queryByTestId("streaming-cursor")).not.toBeInTheDocument();
  });

  it("尝试调用阶段计时：秒数逐秒累加", () => {
    vi.useFakeTimers();
    try {
      render(<MessageItem message={message({ content: "", done: false })} interactive />);
      expect(screen.queryByTestId("streaming-elapsed")).not.toBeInTheDocument();
      act(() => {
        vi.advanceTimersByTime(3000);
      });
      expect(screen.getByTestId("streaming-elapsed")).toHaveTextContent("3s");
      act(() => {
        vi.advanceTimersByTime(62000);
      });
      expect(screen.getByTestId("streaming-elapsed")).toHaveTextContent("1:05");
    } finally {
      vi.useRealTimers();
    }
  });

  it("破坏者视角：首帧到达（有内容）后计时行消失，不残留秒数", () => {
    vi.useFakeTimers();
    try {
      const { rerender } = render(
        <MessageItem message={message({ content: "", done: false })} interactive />,
      );
      act(() => {
        vi.advanceTimersByTime(2000);
      });
      expect(screen.getByTestId("streaming-elapsed")).toHaveTextContent("2s");
      rerender(<MessageItem message={message({ content: "正文来了", done: false })} interactive />);
      expect(screen.queryByTestId("streaming-status")).not.toBeInTheDocument();
      expect(screen.queryByTestId("streaming-elapsed")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("有内容的流式回复：不渲染流式光标", () => {
    const { container } = render(
      <MessageItem message={message({ content: "正在生成中", done: false })} interactive />,
    );
    expect(screen.getByTestId("markdown-body")).toBeInTheDocument();
    expect(container.querySelector(".animate-pulse")).toBeNull();
  });

  it("普通回复身份头使用助理品牌头像", () => {
    render(<MessageItem message={message()} interactive />);
    expect(screen.getByText("AI 助手")).toBeInTheDocument();
    const avatar = screen.getByTestId("assistant-avatar");
    expect(avatar.querySelector("img")).toHaveAttribute("src", "/assistant-avatar.svg");
  });

  it("员工回复用员工名身份头（优先于默认身份）", () => {
    render(
      <MessageItem
        message={message({
          senderName: "市场调研",
          senderRole: "selection-team_market_researcher",
        })}
        interactive
      />,
    );
    expect(screen.getByText("市场调研")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
    expect(screen.queryByTestId("assistant-avatar")).not.toBeInTheDocument();
  });

  it("助理身份覆盖：秘书会话身份头为「秘书」（与项目面板一致；缺省仍「AI 助手」）", () => {
    render(
      <MessageItem
        message={message()}
        interactive
        assistantIdentity={{ name: "秘书", roleId: "secretary" }}
      />,
    );
    expect(screen.getByText("秘书")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
  });

  it("破坏者：assistant 操作条与卡片左缘对齐（随头像左缘对齐，无缩进）", () => {
    // 内容与头像左缘对齐（pl-5 姓名缩进移除）
    render(<MessageItem message={message()} interactive />);
    expect(screen.getByTestId("message-actions").className).not.toContain("pl-5");
  });

  it("user 操作条不缩进（右对齐随卡片右缘）", () => {
    render(<MessageItem message={message({ role: "user", content: "你好" })} interactive />);
    expect(screen.getByTestId("message-actions").className).not.toContain("pl-5");
  });

  it("v5 G3：@提及渲染可点击（data-mention-name）+ 点击回调", async () => {
    const onMentionClick = vi.fn();
    render(
      <MessageItem
        message={message({ content: "@小美 看下这个" })}
        interactive
        mentions={["小美"]}
        onMentionClick={onMentionClick}
      />,
    );
    const mention = await screen.findByText("@小美");
    expect(mention.getAttribute("data-mention-name")).toBe("小美");
    fireEvent.click(mention);
    expect(onMentionClick).toHaveBeenCalledWith("小美");
  });

  it("v5 G1：已上传附件名可点击 → 打开预览页签回调", () => {
    const onAttachmentOpen = vi.fn();
    render(
      <MessageItem
        message={message({
          role: "user",
          content: "看附件",
          attachments: [{ upload_id: "u1", name: "报告.md", size: 12 }],
        })}
        interactive
        onAttachmentOpen={onAttachmentOpen}
      />,
    );
    fireEvent.click(screen.getByTestId("attachment-open-u1"));
    expect(onAttachmentOpen).toHaveBeenCalledWith("u1", "报告.md");
  });

  it("v5 G4：工具明细行点击 → onToolDetail(messageId, nodeId)（行内已无重试按钮）", () => {
    const onToolDetail = vi.fn();
    const onRetryTool = vi.fn();
    render(
      <MessageItem
        message={message({
          process: [
            {
              id: "n1",
              kind: "tool",
              toolName: "bash",
              input: { command: "ls" },
              status: "error",
              detail: "boom",
              toolCallId: "call_9",
            },
          ],
        })}
        interactive
        onToolDetail={onToolDetail}
        onRetryTool={onRetryTool}
      />,
    );
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    fireEvent.click(screen.getByTestId("process-item"));
    expect(onToolDetail).toHaveBeenCalledWith("m1", "n1");
    expect(screen.queryByTestId("process-item-retry")).not.toBeInTheDocument();
    expect(onToolDetail).toHaveBeenCalledTimes(1);
  });
});

describe("MessageItem 渲染层清理模型自写来源清单", () => {
  it("不渲染尾部的引用来源与内部标识", () => {
    render(
      <MessageItem
        message={message({
          content:
            "产品保修期为两年。\n\n引用来源：[1] warranty.md（知识库条目「保修政策」，chunk_id: kchunk:abc）",
        })}
      />,
    );
    expect(screen.getByText(/产品保修期为两年/)).toBeInTheDocument();
    expect(screen.queryByText(/引用来源/)).not.toBeInTheDocument();
    expect(screen.queryByText(/chunk_id/)).not.toBeInTheDocument();
  });
});
