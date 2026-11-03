/**
 * 功能：宿主事件聚合测试（use-host-events）——公司 WS 错误帧必须显式告知
 * 不静默（遗漏修复：消息未持久化 / 修正被拒等此前无任何 UI 反馈）。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useHostEvents } from "@/hooks/use-host-events";
import { socket } from "@/services/ws";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

const wsHarness = vi.hoisted(() => ({
  shellFrame: null as ((frame: unknown) => void) | null,
  sessionFrame: null as ((frame: unknown) => void) | null,
  companyStatus: null as ((status: string) => void) | null,
}));

vi.mock("@/services/ws", () => ({
  socket: {
    onFrame: vi.fn((callback: (frame: unknown) => void) => {
      wsHarness.shellFrame = callback;
      return () => undefined;
    }),
    onStatus: vi.fn((callback: (status: string) => void) => {
      wsHarness.companyStatus = callback;
      return () => undefined;
    }),
    getStatus: vi.fn(() => "open"),
    // 项目订阅经 socket.send 上行
    send: vi.fn(() => true),
  },
  sessionSocket: {
    onStatus: vi.fn(() => () => undefined),
    // -捕获会话通道帧（done/error/interaction 通知源）供 i18n 用例驱动
    onFrame: vi.fn((callback: (frame: unknown) => void) => {
      wsHarness.sessionFrame = callback;
      return () => undefined;
    }),
    getStatus: vi.fn(() => "open"),
  },
}));

/** 功能：thread_progress 帧构造（归属字段齐全——用于过程槽归并/兜底清槽用例）。 */
function progressFrame(eventType: string, data: Record<string, unknown>) {
  return {
    type: "thread_progress",
    data: {
      project_id: "p1",
      thread_id: "t1",
      role_id: "r1",
      event: { type: eventType, data },
    },
  };
}

describe("useHostEvents", () => {
  beforeEach(() => {
    wsHarness.shellFrame = null;
    useChatStore.setState({ notifications: [], employeeProgress: {}, employeeProgressDone: {} });
  });

  it("thread_progress 归并；done 保留完成态过程块；新执行首帧重置重建", () => {
    renderHook(() => useHostEvents());

    act(() => {
      wsHarness.shellFrame?.({
        type: "thread_progress",
        data: {
          project_id: "p1",
          thread_id: "t1",
          role_id: "r1",
          event: { type: "thinking_delta", data: { text: "想" } },
        },
      });
    });
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);

    act(() => {
      wsHarness.shellFrame?.({
        type: "thread_progress",
        data: {
          project_id: "p1",
          thread_id: "t1",
          role_id: "r1",
          event: { type: "done", data: {} },
        },
      });
    });
    // 完成后过程块保留（可回顾），仅置标记
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);

    // 新执行首帧 → 清槽重建（旧节点不残留）
    act(() => {
      wsHarness.shellFrame?.({
        type: "thread_progress",
        data: {
          project_id: "p1",
          thread_id: "t1",
          role_id: "r1",
          event: { type: "thinking_delta", data: { text: "新任务思路" } },
        },
      });
    });
    const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "thought", text: "新任务思路" });
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
  });

  it("破坏者：thread_progress 缺归属字段不得落无主过程槽", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.shellFrame?.({
        type: "thread_progress",
        data: { event: { type: "thinking_delta", data: { text: "x" } } },
      });
    });
    expect(Object.keys(useChatStore.getState().employeeProgress)).toHaveLength(0);
  });

  it("完成态兜底到期清槽（无人消费时过程块不永久悬浮，REV-137b2-2）", () => {
    vi.useFakeTimers();
    try {
      renderHook(() => useHostEvents());
      act(() => {
        wsHarness.shellFrame?.(progressFrame("thinking_delta", { text: "想" }));
      });
      act(() => {
        wsHarness.shellFrame?.(progressFrame("done", {}));
      });
      expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);

      act(() => {
        vi.advanceTimersByTime(120_000);
      });
      expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toBeUndefined();
      expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("窗口常量 = 2min：30s 时完成态过程块仍保留（不提前清）", () => {
    vi.useFakeTimers();
    try {
      renderHook(() => useHostEvents());
      act(() => {
        wsHarness.shellFrame?.(progressFrame("thinking_delta", { text: "想" }));
      });
      act(() => {
        wsHarness.shellFrame?.(progressFrame("done", {}));
      });

      act(() => {
        vi.advanceTimersByTime(30_000);
      });
      // 由 放宽到 2min —— 回复写回晚到时不至于已被兜底清掉
      expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);
      expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("破坏者（REV-137b2-9）：公司链路断开 → 仍在执行的过程槽结算（附断线注记）并被兜底回收", () => {
    vi.useFakeTimers();
    try {
      renderHook(() => useHostEvents());
      act(() => {
        wsHarness.shellFrame?.(progressFrame("thinking_delta", { text: "跑着" }));
      });
      // 断线前：无完成标记（过程块显示「执行中」——错过的终态帧补不回来的那种状态）
      expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();

      act(() => {
        wsHarness.companyStatus?.("reconnecting");
      });
      const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
      expect(nodes[nodes.length - 1]).toMatchObject({ kind: "note", noteType: "disconnected" });
      expect(nodes[0]).toMatchObject({ status: "success" });
      expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);

      // 结算槽同样进入兜底回收（2min）——不永久悬浮
      act(() => {
        vi.advanceTimersByTime(120_000);
      });
      expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("对照组（REV-137b2-9）：链路恢复/保持 open 不触发结算（不误伤在跑的执行）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.shellFrame?.(progressFrame("thinking_delta", { text: "跑着" }));
    });
    act(() => {
      wsHarness.companyStatus?.("open");
    });
    const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(nodes).toHaveLength(1);
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
  });

  it("项目订阅（REV-137b2-3）：链路进入 open 重发 + 选中项目变化重发（137c-B2 后需先握手）", () => {
    renderHook(() => useHostEvents());
    renderHook(() => useHostEvents());
    const send = vi.mocked(socket.send);
    send.mockClear();
    // -握手先于订阅（hello 声明 progress_scope 后才会发 subscribe）
    act(() => {
      wsHarness.shellFrame?.({ type: "hello", data: { capabilities: ["progress_scope"] } });
    });
    send.mockClear();

    // 每次进入 open（首次连接 / 重连 = 新连接，服务端旧订阅已随断连清理）→ 必须重发
    act(() => {
      wsHarness.companyStatus?.("open");
    });
    expect(send).toHaveBeenCalledWith({ type: "subscribe", project_ids: ["company"] });

    // 选中项目变化 → 重发（服务端按连接只发该项目的过程帧）
    send.mockClear();
    act(() => {
      useChatStore.getState().setActiveProject("proj-a");
    });
    expect(send).toHaveBeenCalledWith({ type: "subscribe", project_ids: ["proj-a"] });
  });

  it("破坏者：兜底到期前已开始新执行 → 不得清掉在跑的过程", () => {
    vi.useFakeTimers();
    try {
      renderHook(() => useHostEvents());
      act(() => {
        wsHarness.shellFrame?.(progressFrame("done", {}));
      });
      // 到期前新执行开始（重置槽与完成标记）——留在旧 窗口之外、2min 窗口之内
      act(() => {
        vi.advanceTimersByTime(119_000);
      });
      act(() => {
        wsHarness.shellFrame?.(progressFrame("thinking_delta", { text: "新任务" }));
      });
      // 旧定时器到点：因无完成标记 → no-op
      act(() => {
        vi.advanceTimersByTime(2_000);
      });
      const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
      expect(nodes).toHaveLength(1);
      expect(nodes[0]).toMatchObject({ kind: "thought", text: "新任务", status: "running" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("公司 WS 错误帧（字符串 data）→ 通知中心显式告知", () => {
    renderHook(() => useHostEvents());
    expect(wsHarness.shellFrame).not.toBeNull();

    act(() => {
      wsHarness.shellFrame?.({
        type: "error",
        data: "message not persisted: work item 'x' not found",
      });
    });

    const notifications = useChatStore.getState().notifications;
    expect(notifications).toHaveLength(1);
    expect(notifications[0].kind).toBe("error");
    expect(notifications[0].body).toContain("not found");
  });

  it("公司 WS 错误帧（对象 data）→ 取 message 字段", () => {
    renderHook(() => useHostEvents());

    act(() => {
      wsHarness.shellFrame?.({ type: "error", data: { message: "content required" } });
    });

    const notifications = useChatStore.getState().notifications;
    expect(notifications[0].body).toBe("content required");
  });

  it("resume_gap（断线补发仍有缺口）→ 静默不推通知（噪音）", () => {
    renderHook(() => useHostEvents());

    act(() => {
      wsHarness.shellFrame?.({ type: "resume_gap", from: 11, to: 20 });
    });

    const notifications = useChatStore.getState().notifications;
    expect(notifications).toHaveLength(0);
  });
});

describe("137c-B4 宿主通知 i18n（REV-137b2-10）", () => {
  beforeEach(() => {
    wsHarness.shellFrame = null;
    wsHarness.sessionFrame = null;
    useChatStore.setState({ notifications: [], employeeProgress: {}, employeeProgressDone: {} });
    useUiStore.setState({ locale: "en" });
  });

  it("locale=en：出错/交互通知均为英文（完成/gap 不再推通知）", () => {
    renderHook(() => useHostEvents());
    const cjk = /[\u4e00-\u9fa5]/;
    act(() => {
      wsHarness.sessionFrame?.({
        type: "done",
        data: { session_id: "s1", terminal_reason: "COMPLETED" },
      });
      wsHarness.sessionFrame?.({ type: "error", data: { session_id: "s1", message: "boom" } });
      wsHarness.sessionFrame?.({ type: "interaction", data: { kind: "permission" } });
      wsHarness.shellFrame?.({ type: "resume_gap", from: 1, to: 3 });
    });
    const notifications = useChatStore.getState().notifications;
    // 仅 error 推通知（done / resume_gap / interaction 均静默）
    expect(notifications).toHaveLength(1);
    expect(
      notifications.some((item) => item.kind === "done"),
      "回复完成不得再进通知中心（由会话条目圆点承载）",
    ).toBe(false);
    expect(useChatStore.getState().sessionActivity.s1, "完成必须标记会话活动态").toBe("done");
    for (const item of notifications) {
      expect(item.title, "标题不得含中文：" + item.title).not.toMatch(cjk);
      if (item.body) expect(item.body, "正文不得含中文：" + item.body).not.toMatch(cjk);
    }
  });

  it("破坏者：源码不得残留硬编码中文通知文案（title/body 字面量）", () => {
    const source = readFileSync(resolve(process.cwd(), "src/hooks/use-host-events.ts"), "utf-8");
    const literals = source
      .split("\n")
      .filter((line) => /(title|body)\s*:\s*[`"']/.test(line))
      .filter((line) => /[\u4e00-\u9fa5]/.test(line));
    expect(literals, "硬编码中文通知文案：" + literals.join("、")).toEqual([]);
  });
});

// 发现：宿主通道的 interaction 帧此前**只发通知、不留帧**。
// 会话通道（use-session-stream）只投递「当前活动会话」的帧 → 用户切到别处时
// 该会话的审批帧只经宿主通道到达 → 切回后决策区无卡 → Agent 永久阻塞（死路）。
describe("跨会话审批帧必须留帧（不可只发通知）", () => {
  beforeEach(() => {
    wsHarness.shellFrame = null;
    wsHarness.sessionFrame = null;
    useChatStore.setState({
      notifications: [],
      interactions: {},
      employeeProgress: {},
      employeeProgressDone: {},
    });
    useUiStore.setState({ locale: "zh" });
  });

  it("宿主通道收到 permission 帧 → 既入 store（切回可渲染）不推通知", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.sessionFrame?.({
        type: "interaction",
        data: {
          interaction_id: "i-host-1",
          session_id: "s-other",
          kind: "permission",
          payload: { prompt: "允许执行 write？" },
        },
      });
    });
    const state = useChatStore.getState();
    const frame = state.interactions["i-host-1"];
    expect(frame, "帧必须留在 store，否则切回该会话无卡可渲染").toBeDefined();
    expect(frame.session_id).toBe("s-other");
    expect(frame.kind).toBe("permission");
    expect(state.notifications.some((n) => n.kind === "interaction")).toBe(false);
  });

  it("破坏者：notify 帧仍不得入 store（137c-B1 语义不回归）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.sessionFrame?.({
        type: "interaction",
        data: {
          interaction_id: "i-host-2",
          session_id: "s-other",
          kind: "notify",
          payload: { message: "已自动批准" },
        },
      });
    });
    const state = useChatStore.getState();
    expect(state.interactions["i-host-2"]).toBeUndefined();
    expect(Object.keys(state.interactions)).toHaveLength(0);
  });

  it("破坏者：缺 interaction_id 的帧不入 store（不得制造空键帧）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.sessionFrame?.({
        type: "interaction",
        data: { session_id: "s-other", kind: "permission" },
      });
    });
    expect(Object.keys(useChatStore.getState().interactions)).toHaveLength(0);
  });
});

describe("137c-B2 能力握手（版本错配噪音根治，REV-137b2-12）", () => {
  beforeEach(() => {
    wsHarness.shellFrame = null;
    wsHarness.companyStatus = null;
    useChatStore.setState({
      notifications: [],
      employeeProgress: {},
      employeeProgressDone: {},
      activeProjectId: "",
      sessions: [],
    });
    vi.mocked(socket.send).mockClear();
  });

  it("破坏者：未收到 hello（旧后端）→ open 不发 subscribe（不产生 content required 噪音）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.companyStatus?.("open");
    });
    expect(socket.send).not.toHaveBeenCalled();
  });

  it("hello 声明 progress_scope → 补发订阅（握手在途的意图不丢）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.companyStatus?.("open");
    });
    expect(socket.send).not.toHaveBeenCalled();
    act(() => {
      wsHarness.shellFrame?.({ type: "hello", data: { capabilities: ["progress_scope"] } });
    });
    expect(socket.send).toHaveBeenCalledWith(expect.objectContaining({ type: "subscribe" }));
  });

  it("破坏者：hello 未声明 progress_scope → 不发 subscribe（按声明降级）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.shellFrame?.({ type: "hello", data: { capabilities: ["something_else"] } });
    });
    act(() => {
      wsHarness.companyStatus?.("open");
    });
    expect(socket.send).not.toHaveBeenCalled();
  });
});

// 员工链路多问卡/权限卡（seat_executor 真挂起 → /ws/company 广播）：
// 帧必须入 store（发起会话内渲染决策卡）+ 通知中心提示；notify 盲帧不占决策槽。
describe("员工交互帧经公司 WS 到达（多问卡真挂起恢复）", () => {
  beforeEach(() => {
    wsHarness.shellFrame = null;
    wsHarness.sessionFrame = null;
    useChatStore.setState({
      notifications: [],
      interactions: {},
      employeeProgress: {},
      employeeProgressDone: {},
    });
  });

  it("公司 WS interaction 帧 → 入 store（按会话渲染）不推通知", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.shellFrame?.({
        type: "interaction",
        data: {
          interaction_id: "i-emp-1",
          session_id: "sess-team",
          kind: "input",
          payload: { prompt: "目标市场？", choices: ["美国", "日本"] },
        },
      });
    });
    const state = useChatStore.getState();
    const frame = state.interactions["i-emp-1"];
    expect(frame, "员工多问卡必须入 store（否则会话内无卡可渲染）").toBeDefined();
    expect(frame.session_id).toBe("sess-team");
    expect(frame.kind).toBe("input");
    expect(state.notifications.some((n) => n.kind === "interaction")).toBe(false);
  });

  it("破坏者：notify 帧不得入 store（不占决策槽）", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.shellFrame?.({
        type: "interaction",
        data: {
          interaction_id: "i-emp-notify",
          session_id: "sess-team",
          kind: "notify",
          payload: { notify: true, message: "进度提示" },
        },
      });
    });
    expect(Object.keys(useChatStore.getState().interactions)).toHaveLength(0);
  });

  it("破坏者：缺 interaction_id 的帧不入 store", () => {
    renderHook(() => useHostEvents());
    act(() => {
      wsHarness.shellFrame?.({
        type: "interaction",
        data: { session_id: "sess-team", kind: "permission" },
      });
    });
    expect(Object.keys(useChatStore.getState().interactions)).toHaveLength(0);
  });
});
