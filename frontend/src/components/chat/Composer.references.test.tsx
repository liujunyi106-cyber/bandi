/**
 * 功能：Composer 引用文件行为测试（用户驱动）。
 *
 * 契约：
 * 1. @ 选择项目文件 → 出现「引用 chip」（不再仅文本注入）
 * 2. ⊕→「引用对话中的文件」选择项目文件 → 引用 chip 出现
 * （回归守卫：原实现 replaceTrigger 在 textarea 无 @ 时直接 return，选择后静默失效）
 * 3. 发送：引用转 `@路径` 文本注入 + chips 清除
 * 4. 分组标题本地化（「项目文件」——原实现 key 路径错误显示 raw key）
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/chat/Composer";
import { useChatStore } from "@/stores/chat-store";

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      employees: vi.fn(async () => ({ employees: [] })),
      files: vi.fn(async () => ({
        files: [{ path: "AGENTS.md", name: "AGENTS.md", is_dir: false }],
        root_dir: "",
      })),
    },
  };
});

vi.mock("@/services/settings", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/settings")>();
  return {
    ...actual,
    settingsApi: {
      ...actual.settingsApi,
      model: vi.fn(async () => ({
        available_models: [],
        current_model: "",
        effective_model: "",
        active_profile: null,
      })),
      profiles: vi.fn(async () => ({ active_profile_id: "", profiles: [] })),
    },
  };
});

function renderComposer(onSend: (text: string, attachments?: unknown[]) => void) {
  return render(
    <Composer
      busy={false}
      enhanced
      projectId="p1"
      onSend={onSend}
      onSteer={vi.fn()}
      onStop={vi.fn()}
    />,
  );
}

describe("Composer 文件引用（@ / ⊕ 双入口）", () => {
  beforeEach(() => {
    useChatStore.setState({ draftAttachments: [], draftReferences: [], activeSessionId: "s1" });
  });

  it("@ 选择项目文件 → 引用 chip 出现（发送前可管理，不再只注入文本）", async () => {
    const user = userEvent.setup();
    renderComposer(vi.fn());
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "@");

    // 菜单出现（含本地化分组标题）
    expect(await screen.findByTestId("composer-mentions")).toBeInTheDocument();
    expect(await screen.findByText("项目文件")).toBeInTheDocument();

    const item = await screen.findByTestId("mention-item-file-AGENTS.md");
    await user.click(item);

    expect(await screen.findByTestId("reference-chip")).toHaveTextContent("AGENTS.md");
    // 不残留 @query 文本
    expect((textarea as HTMLTextAreaElement).value).toBe("");
  });

  it("⊕→引用对话中的文件 → 选择项目文件同样出引用 chip（回归：原实现静默失效）", async () => {
    const user = userEvent.setup();
    renderComposer(vi.fn());
    await user.click(screen.getByTestId("composer-plus"));
    await user.click(await screen.findByTestId("composer-plus-quote"));

    // 经 ⊕ 入口打开 @ 面板：textarea 中无 @ trigger
    const item = await screen.findByTestId("mention-item-file-AGENTS.md");
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("");
    await user.click(item);

    expect(await screen.findByTestId("reference-chip")).toHaveTextContent("AGENTS.md");
  });

  it("发送：引用转 @路径 文本注入消息 + chips 清除", async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    renderComposer(onSend);
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "请读一下这份文档");
    // 经 ⊕ 选项目文件
    await user.click(screen.getByTestId("composer-plus"));
    await user.click(await screen.findByTestId("composer-plus-quote"));
    await user.click(await screen.findByTestId("mention-item-file-AGENTS.md"));
    await screen.findByTestId("reference-chip");

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("请读一下这份文档 @AGENTS.md");
    await waitFor(() => expect(screen.queryByTestId("reference-chip")).not.toBeInTheDocument());
  });

  it("引用可移除（chip × 按钮）", async () => {
    const user = userEvent.setup();
    renderComposer(vi.fn());
    await user.click(screen.getByTestId("composer-plus"));
    await user.click(await screen.findByTestId("composer-plus-quote"));
    await user.click(await screen.findByTestId("mention-item-file-AGENTS.md"));
    const chip = await screen.findByTestId("reference-chip");
    await user.click(chip.querySelector("button") as HTMLButtonElement);
    await waitFor(() => expect(screen.queryByTestId("reference-chip")).not.toBeInTheDocument());
  });
});
