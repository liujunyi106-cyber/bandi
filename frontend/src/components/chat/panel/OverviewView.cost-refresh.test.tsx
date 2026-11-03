/**
 * 功能：右栏概览「同会话新轮次后用量刷新」契约。
 * 缺陷：costDetail 只在 sessionId 变化时拉取 ⇒ 同一会话内新轮次完成后
 * 用量卡恒显示 0（两轮真实 LLM 调用后仍 0 tokens / ¥0.00
 * 而后端 cost-detail 已有 28187 tokens / ¥0.028）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OverviewView } from "@/components/chat/panel/OverviewView";
import type { PanelScope } from "@/lib/panel-scope";
import { useChatStore, type TaskLedgerItem } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

const SESSION_SCOPE: PanelScope = { kind: "session", key: "session:s1", sessionId: "s1" };

const { costDetail } = vi.hoisted(() => ({
  costDetail: vi.fn(async () => ({
    session_id: "s1",
    total_tokens: 28187,
    total_cost_cny: 0.028,
    per_model: {},
    tool_stats: {},
  })),
}));

vi.mock("@/services/panel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/panel")>();
  return {
    ...actual,
    panelApi: {
      ...actual.panelApi,
      skills: vi.fn(async () => ({ skills: [] })),
      sop: vi.fn(async () => ({ session_id: "s1", sop: null })),
      costDetail,
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      artifacts: vi.fn(async () => ({ session_id: "s1", artifacts: [] })),
    },
  };
});

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      projects: vi.fn(async () => ({ projects: [] })),
      workItems: vi.fn(async () => ({ items: [], column_counts: {} })),
    },
  };
});

const emptyTasks: TaskLedgerItem[] = [];

describe("OverviewView · 用量刷新", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({
      activeProjectId: "",
      tasksBySession: { s1: emptyTasks },
      messagesBySession: { s1: [] },
    });
    usePanelStore.setState({
      open: true,
      view: "overview",
      taskFocusId: "",
      tabs: [],
      activeTabId: "",
      fileFocusPath: "",
    });
  });

  it("纯文本轮（任务台账不变）也重新拉取用量 —— FM-99 回归守卫", async () => {
    // 复现：纯文本回复**不产生任务台账变化**，原实现以 tasks.length 为重拉信号
    // ⇒ 同一会话内新回复后用量卡恒显示进入会话那一刻的值（API 已有 ¥0.015、面板 ¥0.00）。
    costDetail
      .mockResolvedValueOnce({
        session_id: "s1",
        total_tokens: 0,
        total_cost_cny: 0,
        per_model: {},
        tool_stats: {},
      } as never)
      .mockResolvedValue({
        session_id: "s1",
        total_tokens: 28187,
        total_cost_cny: 0.028,
        per_model: {},
        tool_stats: {},
      } as never);

    const { rerender } = render(<OverviewView scope={SESSION_SCOPE} />);
    await waitFor(() => expect(costDetail).toHaveBeenCalled());
    const callsBefore = costDetail.mock.calls.length;

    // 新一轮纯文本回复落地：消息数 +1，**任务台账保持为空**
    useChatStore.setState({
      tasksBySession: { s1: emptyTasks },
      messagesBySession: {
        s1: [
          { id: "u1", sessionId: "s1", role: "user", content: "问题", done: true },
          { id: "a1", sessionId: "s1", role: "assistant", content: "答案", done: true },
        ],
      },
    });
    rerender(<OverviewView scope={SESSION_SCOPE} />);

    await waitFor(() => expect(costDetail.mock.calls.length).toBeGreaterThan(callsBefore));
    expect(await screen.findByText("28.2k")).toBeInTheDocument();
  });

  it("有工具的轮次（消息 + 任务台账同时变化）同样触发重拉", async () => {
    costDetail
      .mockResolvedValueOnce({
        session_id: "s1",
        total_tokens: 0,
        total_cost_cny: 0,
        per_model: {},
        tool_stats: {},
      } as never)
      .mockResolvedValue({
        session_id: "s1",
        total_tokens: 28187,
        total_cost_cny: 0.028,
        per_model: {},
        tool_stats: {},
      } as never);

    const { rerender } = render(<OverviewView scope={SESSION_SCOPE} />);
    await waitFor(() => expect(costDetail).toHaveBeenCalled());

    useChatStore.setState({
      tasksBySession: { s1: [{ id: "t1", title: "调研", content: "", status: "completed" }] },
      messagesBySession: {
        s1: [
          { id: "u1", sessionId: "s1", role: "user", content: "问题", done: true },
          { id: "a1", sessionId: "s1", role: "assistant", content: "答案", done: true },
        ],
      },
    });
    rerender(<OverviewView scope={SESSION_SCOPE} />);

    await waitFor(() => expect(costDetail.mock.calls.length).toBeGreaterThan(1));
    expect(await screen.findByText("28.2k")).toBeInTheDocument();
  });

  it("消息数不变、仅终帧到达时也重拉 —— 本轮 token 必须计入", async () => {
    // 真实时序：assistant 消息在流式首字就进 store（消息数已 +1），成本台账在
    // 轮末才刷库。只挂消息数会早于刷库取到旧值，且此后无信号 ⇒ 停在上一轮数字。
    const messages = [
      { id: "u1", sessionId: "s1", role: "user", content: "问题", done: true },
      { id: "a1", sessionId: "s1", role: "assistant", content: "答案", done: true },
    ];
    useChatStore.setState({
      tasksBySession: { s1: emptyTasks },
      messagesBySession: { s1: messages },
      sessionActivity: {},
    });
    costDetail.mockResolvedValue({
      session_id: "s1",
      total_tokens: 28187,
      total_cost_cny: 0.028,
      per_model: {},
      tool_stats: {},
    } as never);

    const { rerender } = render(<OverviewView scope={SESSION_SCOPE} />);
    await waitFor(() => expect(costDetail).toHaveBeenCalled());
    const callsBefore = costDetail.mock.calls.length;

    // 终帧到达：消息数组原地不变，只有活动态 running→done
    useChatStore.setState({ sessionActivity: { s1: "done" } });
    rerender(<OverviewView scope={SESSION_SCOPE} />);

    await waitFor(() => expect(costDetail.mock.calls.length).toBeGreaterThan(callsBefore));
  });
});
