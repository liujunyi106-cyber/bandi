/**
 * 契约：流看门狗 —— 后端终止但未发 `done`/`error` 帧时必须本地收尾。
 *
 * 证据（turn 17， 12:25-12:26）：后端 51.8s 断流 → 状态机终止
 * 但未下发 `done` 帧 ⇒ 前端 `stream` 槽永不释放 ⇒ UI 永久「正在回复中…」。
 * 原兜底 `abortLocalStream` 只由「用户按停止」或「abort 404」触发，**无被动检测**。
 *
 * 钉行为不钉实现：断言「静默超时后流槽被释放」，不断言定时器内部结构。
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStream, STREAM_SILENCE_TIMEOUT_MS } from "./use-session-stream";
import { useChatStore } from "@/stores/chat-store";

const listeners: { frame?: (f: unknown) => void; scope?: (s: string) => void } = {};

vi.mock("@/services/ws", () => ({
  sessionSocket: {
    connect: vi.fn(),
    onFrame: (cb: (f: unknown) => void) => {
      listeners.frame = cb;
      return () => {
        listeners.frame = undefined;
      };
    },
    onScope: (cb: (s: string) => void) => {
      listeners.scope = cb;
      return () => {
        listeners.scope = undefined;
      };
    },
    setResumeScope: vi.fn(),
    off: vi.fn(),
    close: vi.fn(),
    getStatus: vi.fn(() => "open"),
  },
}));

vi.mock("@/services/sessions", () => ({
  sessionsApi: {
    list: vi.fn(async () => []),
    detail: vi.fn(async () => ({ messages: [] })),
  },
}));

describe("流看门狗", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    listeners.frame = undefined;
    useChatStore.setState({
      streams: {
        s1: {
          sessionId: "s1",
          pendingId: "p1",
        },
      } as never,
      messagesBySession: {
        s1: [
          { id: "p1", sessionId: "s1", role: "assistant", content: "半句", done: false },
        ] as never,
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("静默超时后释放流槽（不再永久「正在回复中」）", async () => {
    renderHook(() => useSessionStream());
    expect(useChatStore.getState().streams["s1"]).toBeDefined();

    await act(async () => {
      vi.advanceTimersByTime(STREAM_SILENCE_TIMEOUT_MS + 6_000);
    });

    // 假时钟下 waitFor 不可用（其内部用真实定时器），直接读状态
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
  });

  it("已产出内容时收尾为 done（保留半句，不丢结果）", async () => {
    renderHook(() => useSessionStream());
    await act(async () => {
      vi.advanceTimersByTime(STREAM_SILENCE_TIMEOUT_MS + 6_000);
    });
    const msg = useChatStore.getState().messagesBySession.s1?.[0];
    expect(msg?.done).toBe(true);
    expect(msg?.content).toBe("半句");
  });

  it("看门狗收尾带 watchdog 注记（有产出时结局可解释）", async () => {
    renderHook(() => useSessionStream());
    await act(async () => {
      vi.advanceTimersByTime(STREAM_SILENCE_TIMEOUT_MS + 6_000);
    });
    const msg = useChatStore.getState().messagesBySession.s1?.[0];
    const notes = (msg?.process ?? []).filter(
      (node) => node.kind === "note" && node.noteType === "watchdog",
    );
    expect(notes.length).toBe(1);
  });

  it("空占位不加注记（幽灵消息仍被移除）", async () => {
    useChatStore.setState({
      messagesBySession: {
        s1: [
          { id: "p1", sessionId: "s1", role: "assistant", content: "   ", done: false },
        ] as never,
      },
    });
    renderHook(() => useSessionStream());
    await act(async () => {
      vi.advanceTimersByTime(STREAM_SILENCE_TIMEOUT_MS + 6_000);
    });
    const msg = useChatStore.getState().messagesBySession.s1?.find((m) => m.id === "p1");
    expect(msg).toBeUndefined();
  });

  it("空占位被移除（不留幽灵消息）", async () => {
    useChatStore.setState({
      messagesBySession: {
        s1: [
          { id: "p1", sessionId: "s1", role: "assistant", content: "   ", done: false },
        ] as never,
      },
    });
    renderHook(() => useSessionStream());
    await act(async () => {
      vi.advanceTimersByTime(STREAM_SILENCE_TIMEOUT_MS + 6_000);
    });
    expect(useChatStore.getState().messagesBySession.s1?.some((m) => m.id === "p1")).toBe(false);
  });

  it("帧持续到达时不得误杀（静默计时被重置）", async () => {
    renderHook(() => useSessionStream());
    // 每 来一帧，永不静默
    for (let i = 0; i < 8; i += 1) {
      await act(async () => {
        vi.advanceTimersByTime(10_000);
        listeners.frame?.({ type: "content_delta", data: { text: "x" }, session_id: "s1" });
      });
    }
    expect(useChatStore.getState().streams["s1"]).toBeDefined();
  });

  it("无流槽时看门狗空转（不误动其它会话）", async () => {
    useChatStore.setState({ streams: {} });
    renderHook(() => useSessionStream());
    await act(async () => {
      vi.advanceTimersByTime(STREAM_SILENCE_TIMEOUT_MS * 3);
    });
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
  });

  it("team_routed 终帧标记成功终态", async () => {
    renderHook(() => useSessionStream());
    await act(async () => {
      listeners.frame?.({
        type: "done",
        data: { terminal_reason: "team_routed", session_id: "s1" },
      });
    });
    // 团队接管 = 成功移交——终态记录使横幅档位为 none（不显示重试）
    expect(useChatStore.getState().sessionTerminals["s1"]).toBe("team_routed");
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
  });
});
