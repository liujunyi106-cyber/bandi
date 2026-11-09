/**
 * 功能：CommandPalette 行为测试（-3e）——过滤 / 新建会话 / 会话搜索跳转 / 空态。
 * 钉行为不钉实现：断言列表项渲染与选择后的 store 副作用。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPalette } from "@/components/layout/CommandPalette";
import { companyApi } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: { ...actual.companyApi, employees: vi.fn() },
  };
});

const mocked = vi.mocked(companyApi);

function renderPalette(open: boolean) {
  return render(
    <MemoryRouter>
      <CommandPalette open={open} onClose={() => undefined} />
    </MemoryRouter>,
  );
}

describe("CommandPalette（137b-3e）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocked.employees.mockResolvedValue({
      employees: [
        { employee_id: "e1", name: "小美", role_id: "worker" },
        { employee_id: "e2", name: "老王", role_id: "engineer" },
      ],
    });
    useChatStore.setState({
      sessions: [
        {
          id: "s1",
          title: "竞品分析",
          created_at: "2026-09-25T01:00:00",
          message_count: 3,
          project_id: "p1",
        },
        {
          id: "s2",
          title: "随手记",
          created_at: "2026-09-25T02:00:00",
          message_count: 1,
          project_id: "",
        },
      ],
    });
  });

  it("closed 时不渲染；open 渲染输入框 + 新建会话 + 会话列表 + 员工", async () => {
    const { rerender } = renderPalette(false);
    expect(screen.queryByTestId("command-palette")).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <CommandPalette open onClose={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("command-palette")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByTestId("palette-item").length).toBeGreaterThan(2));
    expect(screen.getByText("新建会话")).toBeInTheDocument();
    expect(screen.getByText("竞品分析")).toBeInTheDocument();
    expect(screen.getByText("小美")).toBeInTheDocument();
  });

  it("输入过滤：只留命中项；全不命中 → 空态文案", async () => {
    renderPalette(true);
    await waitFor(() => expect(screen.getAllByTestId("palette-item").length).toBeGreaterThan(0));
    fireEvent.change(screen.getByTestId("palette-input"), { target: { value: "竞品" } });
    expect(screen.getByText("竞品分析")).toBeInTheDocument();
    expect(screen.queryByText("随手记")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("palette-input"), { target: { value: "不存在xyz" } });
    expect(screen.getByText("无匹配结果")).toBeInTheDocument();
  });

  it("破坏者：员工拉取失败 → 面板仍可用（降级为会话/动作搜索）", async () => {
    mocked.employees.mockRejectedValueOnce(new Error("boom"));
    renderPalette(true);
    await waitFor(() => expect(screen.getAllByTestId("palette-item").length).toBeGreaterThan(0));
    expect(screen.getByText("新建会话")).toBeInTheDocument();
  });
});
