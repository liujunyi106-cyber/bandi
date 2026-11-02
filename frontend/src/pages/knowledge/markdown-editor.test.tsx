// 功能：块编辑器契约——格式面板分组、列表续项、自动保存回写 Markdown。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MarkdownEditor } from "@/pages/knowledge/MarkdownEditor";

describe("MarkdownEditor", () => {
  it("格式面板含对齐/复制/删除三组操作", async () => {
    render(<MarkdownEditor initialText={"# 标题\n"} onSave={() => undefined} />);
    await userEvent.click(screen.getByLabelText("格式"));
    expect(await screen.findByText("居中")).toBeInTheDocument();
    expect(screen.getByText("复制")).toBeInTheDocument();
    expect(screen.getByText("删除")).toBeInTheDocument();
    expect(screen.getByText("减少缩进")).toBeInTheDocument();
  });

  it("输入后 600ms 防抖自动保存（序列化回 Markdown）", async () => {
    const onSave = vi.fn();
    render(<MarkdownEditor initialText={"正文\n"} onSave={onSave} />);
    const block = screen.getAllByRole("textbox")[0];
    block.textContent = "新的正文";
    fireEvent.input(block);
    await waitFor(() => expect(onSave).toHaveBeenCalled(), { timeout: 2000 });
    expect(String(onSave.mock.calls.at(-1)?.[0])).toContain("新的正文");
  });

  it("列表内回车继续同一列表类型", async () => {
    const onSave = vi.fn();
    render(<MarkdownEditor initialText={"- 第一项\n"} onSave={onSave} />);
    const block = screen.getAllByRole("textbox")[0];
    block.textContent = "第一项";
    fireEvent.input(block);
    fireEvent.keyDown(block, { key: "Enter" });
    await waitFor(() => {
      const serialized = String(onSave.mock.calls.at(-1)?.[0] ?? "");
      expect(serialized).toContain("- 第一项");
      expect(serialized.split("- ").length).toBeGreaterThan(2);
    });
  });

  it("自动保存失败后重试一次", async () => {
    const onSave = vi.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValue(undefined);
    render(<MarkdownEditor initialText={"正文\n"} onSave={onSave} />);
    const block = screen.getAllByRole("textbox")[0];
    block.textContent = "重试正文";
    fireEvent.input(block);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2), { timeout: 5000 });
  });
});
