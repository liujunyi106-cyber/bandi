import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationMenu } from "@/components/layout/NotificationMenu";
import { memoryApprovalApi } from "@/services/approvals";
import { useChatStore } from "@/stores/chat-store";

vi.mock("@/services/approvals", () => ({
  memoryApprovalApi: {
    list: vi.fn(),
    approve: vi.fn(),
    reject: vi.fn(),
  },
}));

const mocked = vi.mocked(memoryApprovalApi);

describe("NotificationMenu 记忆审批", () => {
  beforeEach(() => {
    useChatStore.setState({ notifications: [] });
    mocked.list.mockResolvedValue({
      pending: [
        {
          id: "m1",
          subsystem: "memory",
          action: "record",
          summary: "长期记忆 A",
          draft: { kind: "长期记忆", target: "长期记忆" },
        },
      ],
    });
  });

  it("只保留长期记忆/草稿记忆/通知，不再出现团队记忆/技能提名", async () => {
    render(
      <MemoryRouter>
        <NotificationMenu />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByTestId("notification-trigger"));

    expect(await screen.findByText("长期记忆")).toBeInTheDocument();
    expect(screen.getByText("草稿记忆")).toBeInTheDocument();
    expect(screen.getByText("通知")).toBeInTheDocument();
    expect(screen.queryByText("团队记忆")).not.toBeInTheDocument();
    expect(screen.queryByText("技能提名")).not.toBeInTheDocument();
    expect(await screen.findByTestId("approval-row-personal")).toHaveTextContent("长期记忆 A");
  });

  it(
    "草稿池非空时提示晋级规则，且不出现在审批列表里",
    async () => {
      mocked.list.mockResolvedValue({ pending: [], drafts: 3 });
      render(
        <MemoryRouter>
          <NotificationMenu />
        </MemoryRouter>,
      );
      fireEvent.click(screen.getByTestId("notification-trigger"));
      expect(await screen.findByTestId("draft-pool-hint")).toHaveTextContent("草稿池还有 3 条待晋级");
      expect(screen.queryByTestId("approval-row-personal")).not.toBeInTheDocument();
    },
  );

  it(
    "冲突裁决卡同时给出旧值与新值",
    async () => {
      mocked.list.mockResolvedValue({
        pending: [
          {
            id: "c1",
            subsystem: "memory",
            action: "store_long_term",
            summary: "发布分支变更",
            conflict: true,
            old_value: "旧值：发布分支是 release/0.2",
            content: "新值：发布分支改成 release/0.3",
            diff: { old: "旧值：发布分支是 release/0.2", new: "新值：发布分支改成 release/0.3" },
          },
        ],
      });
      render(
        <MemoryRouter>
          <NotificationMenu />
        </MemoryRouter>,
      );
      fireEvent.click(screen.getByTestId("notification-trigger"));
      fireEvent.click(await screen.findByTestId("approval-row-personal"));
      expect(await screen.findByTestId("approval-diff-old")).toHaveTextContent("release/0.2");
      expect(screen.getByTestId("approval-diff-new")).toHaveTextContent("release/0.3");
    },
  );
});