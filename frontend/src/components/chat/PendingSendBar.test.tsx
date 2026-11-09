import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PendingSendBar } from "@/components/chat/PendingSendBar";
import type { ChatAttachment } from "@/stores/chat-store";

function item(id: string, text: string, attachments: ChatAttachment[] = []) {
  return { id, text, attachments, createdAt: "2026-11-02T00:00:00" };
}

describe("PendingSendBar", () => {
  it("文本条目执行中可立即发送，并可编辑/删除", () => {
    const onSendNow = vi.fn();
    const onEdit = vi.fn();
    const onDelete = vi.fn();
    const pending = item("p1", "先发这一句");
    render(
      <PendingSendBar
        items={[pending]}
        executing
        onSendNow={onSendNow}
        onEdit={onEdit}
        onDelete={onDelete}
      />,
    );

    expect(screen.getByTestId("pending-send-text")).toHaveTextContent("先发这一句");
    expect(screen.getByTestId("pending-send-prefix").querySelector("svg")).toBeInTheDocument();
    const sendNow = screen.getByTestId("pending-send-now");
    expect(sendNow).toBeEnabled();
    fireEvent.click(sendNow);
    expect(onSendNow).toHaveBeenCalledWith(pending);

    fireEvent.click(screen.getByTestId("pending-send-edit"));
    expect(onEdit).toHaveBeenCalledWith(pending);
    fireEvent.click(screen.getByTestId("pending-send-delete"));
    expect(onDelete).toHaveBeenCalledWith(pending);
  });

  it("带附件条目执行中禁用立即发送，提示等本轮结束", () => {
    const pending = item("p2", "", [{ upload_id: "u1", name: "a.md", size: 1 }]);
    render(
      <PendingSendBar
        items={[pending]}
        executing
        onSendNow={vi.fn()}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(screen.getByTestId("pending-send-text")).toHaveTextContent("附件");
    expect(screen.getByTestId("pending-send-now")).toBeDisabled();
    expect(screen.getByTestId("pending-send-now")).toHaveAttribute(
      "title",
      "附件需等本轮结束后发送",
    );
  });

  it("非执行态附件条目可立即发送（自动发送前的兜底）", () => {
    const onSendNow = vi.fn();
    const pending = item("p3", "带附件", [{ upload_id: "u1", name: "a.md", size: 1 }]);
    render(
      <PendingSendBar
        items={[pending]}
        executing={false}
        onSendNow={onSendNow}
        onEdit={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("pending-send-now"));
    expect(onSendNow).toHaveBeenCalledWith(pending);
  });
});
