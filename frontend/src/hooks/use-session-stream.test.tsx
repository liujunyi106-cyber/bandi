/**
 * 功能：会话流式接线行为测试。
 * 语义：被拒回合（SESSION_BUSY）的 done 帧不承载流式槽——不得终结进行中的回合
 * 否则 `finalizeStream` 会清空 stream 槽，进行中回复的后续增量全被丢弃（回复被截断）。
 */
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionStream } from "@/hooks/use-session-stream";
import { sessionsApi } from "@/services/sessions";
import { sessionSocket } from "@/services/ws";
import { useChatStore } from "@/stores/chat-store";

const frameHarness = vi.hoisted(() => ({
  handler: null as ((frame: unknown) => void) | null,
}));

vi.mock("@/services/ws", () => ({
  sessionSocket: {
    onFrame: vi.fn((callback: (frame: unknown) => void) => {
      frameHarness.handler = callback;
      return () => undefined;
    }),
    connect: vi.fn(),
    // 10.10：会话通道 resume 作用域（本组旧用例不涉及补发协议，仅需方法存在）
    setResumeScope: vi.fn(),
  },
}));

vi.mock("@/services/sessions", () => ({
  sessionsApi: {
    list: vi.fn(async () => []),
    // 10.10：resumed 后重载历史补齐正文
    detail: vi.fn(async () => ({ messages: [] })),
  },
}));

function emit(frame: unknown): void {
  frameHarness.handler?.(frame);
}

describe("useSessionStream · done 帧定稿口径", () => {
  beforeEach(() => {
    frameHarness.handler = null;
    useChatStore.setState({
      sessions: [],
      sessionsLoaded: false,
      activeSessionId: "s1",
      activeProjectId: "",
      messagesBySession: {},
      streams: {},
      tasksBySession: {},
      interactions: {},
      notifications: [],
      stagedRevert: null,
    });
    renderHook(() => useSessionStream());
  });

  it("破坏者：SESSION_BUSY 的 done 不终结流式槽（进行中回复不被截断）", () => {
    useChatStore.getState().appendLocalUser("s1", "重发我");
    const pendingId = useChatStore.getState().streams["s1"]?.pendingId;
    expect(pendingId).toBeTruthy();

    emit({
      type: "done",
      data: { session_id: "s1", terminal_reason: "SESSION_BUSY", turn_index: 0 },
    });

    const after = useChatStore.getState();
    expect(after.streams["s1"]?.pendingId).toBe(pendingId);
    const pending = (after.messagesBySession["s1"] ?? []).find((item) => item.id === pendingId);
    expect(pending?.done).toBe(false);

    // 关键：被拒回合之后的增量仍落在原槽（修复前 stream 槽已为 null → 增量全丢）
    emit({ type: "content_delta", data: { text: "进行中内容" } });
    const updated = useChatStore
      .getState()
      .messagesBySession["s1"].find((item) => item.id === pendingId);
    expect(updated?.content).toContain("进行中内容");

    // 用户可见提示（不静默吞）
    expect(
      (after.messagesBySession["s1"] ?? []).some(
        (item) => item.role === "system" && item.content.length > 0,
      ),
    ).toBe(true);
  });

  it("对照组：正常 done 仍定稿并释放槽（防过度收紧）", () => {
    useChatStore.getState().appendLocalUser("s1", "你好");

    emit({
      type: "done",
      data: { session_id: "s1", terminal_reason: "COMPLETED", turn_index: 1 },
    });

    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
    const list = useChatStore.getState().messagesBySession["s1"] ?? [];
    expect(list.every((item) => item.done)).toBe(true);
  });

  it("团队接管：team_routed 的 done 移除空占位助理气泡并释放槽（员工产出走线程回写）", () => {
    useChatStore.getState().appendLocalUser("s1", "你好");
    const pendingId = useChatStore.getState().streams["s1"]?.pendingId;
    expect(pendingId).toBeTruthy();

    emit({
      type: "done",
      data: { session_id: "s1", terminal_reason: "team_routed", turn_index: 0 },
    });

    const after = useChatStore.getState();
    expect(after.streams["s1"]).toBeUndefined();
    const list = after.messagesBySession["s1"] ?? [];
    expect(list.some((item) => item.id === pendingId)).toBe(false);
    expect(list.some((item) => item.role === "user" && item.content === "你好")).toBe(true);
  });

  it("interaction 帧 → 挂起交互入 store（决策卡可渲染的接线，137b-3 测试缺口补齐）", () => {
    emit({
      type: "interaction",
      data: {
        interaction_id: "i9",
        session_id: "s1",
        kind: "input",
        payload: {
          questions: [
            { id: "q1", question: "?" },
            { id: "q2", question: "??" },
          ],
        },
        expires_in: 120,
      },
    });
    const frame = useChatStore.getState().interactions["i9"];
    expect(frame).toBeDefined();
    expect(frame.kind).toBe("input");
    expect(frame.expires_in).toBe(120);
  });
});

describe("137c-B1 notify 分流（REV-137b1-10）", () => {
  beforeEach(() => {
    frameHarness.handler = null;
    useChatStore.setState({
      sessions: [],
      sessionsLoaded: false,
      activeSessionId: "s1",
      messagesBySession: {},
      streams: {},
      interactions: {},
      notifications: [],
    });
    renderHook(() => useSessionStream());
  });

  it("notify 交互帧 → 通知中心，且不进挂起交互槽", () => {
    emit({
      type: "interaction",
      data: {
        interaction_id: "i-notify",
        session_id: "s1",
        kind: "notify",
        payload: { message: "已自动批准 bash（本会话始终允许）", notify: true },
      },
    });
    const state = useChatStore.getState();
    expect(state.interactions["i-notify"]).toBeUndefined();
    expect(Object.keys(state.interactions)).toHaveLength(0);
    expect(state.notifications).toHaveLength(1);
    expect(state.notifications[0].title).toBe("已自动批准 bash（本会话始终允许）");
    expect(state.notifications[0].kind).toBe("info");
  });

  it("破坏者：旧后端形态（kind=permission + payload.notify）同样分流；notify 不遮蔽后续真实审批卡", () => {
    emit({
      type: "interaction",
      data: {
        interaction_id: "i-legacy",
        session_id: "s1",
        kind: "permission",
        payload: { message: "旧形态通知", notify: true },
      },
    });
    emit({
      type: "interaction",
      data: {
        interaction_id: "i-perm",
        session_id: "s1",
        kind: "permission",
        payload: { ui_variant: "permission", prompt: "允许？" },
      },
    });
    const state = useChatStore.getState();
    expect(Object.keys(state.interactions)).toEqual(["i-perm"]);
    expect(state.notifications).toHaveLength(1);
  });
});

describe("useSessionStream · 10.10 断线补发后补齐正文", () => {
  beforeEach(() => {
    frameHarness.handler = null;
    vi.mocked(sessionsApi.detail).mockClear();
    vi.mocked(sessionSocket.setResumeScope).mockClear();
    useChatStore.setState({
      sessions: [],
      sessionsLoaded: false,
      activeSessionId: "s1",
      activeProjectId: "",
      messagesBySession: {},
      streams: {},
      tasksBySession: {},
      interactions: {},
      notifications: [],
      stagedRevert: null,
    });
    renderHook(() => useSessionStream());
  });

  it("resumed 有补发 → 重载该会话历史（服务端轮末已落库，补回断线期间正文）", async () => {
    useChatStore.getState().appendLocalUser("s1", "你好");

    emit({ type: "resumed", replayed: 1, last_seq: 7 });

    await vi.waitFor(() => expect(vi.mocked(sessionsApi.detail)).toHaveBeenCalledWith("s1"));
  });

  it("破坏者：replayed=0 不重载历史（无补发即无缺口，避免无谓请求）", async () => {
    useChatStore.getState().appendLocalUser("s1", "你好");

    emit({ type: "resumed", replayed: 0, last_seq: 0 });
    await Promise.resolve();

    expect(vi.mocked(sessionsApi.detail)).not.toHaveBeenCalled();
  });

  it("resume 作用域随当前会话同步（切会话即更新，重连按新会话补发）", () => {
    expect(vi.mocked(sessionSocket.setResumeScope)).toHaveBeenLastCalledWith("s1");

    useChatStore.setState({ activeSessionId: "s2" });

    expect(vi.mocked(sessionSocket.setResumeScope)).toHaveBeenLastCalledWith("s2");
  });
});
