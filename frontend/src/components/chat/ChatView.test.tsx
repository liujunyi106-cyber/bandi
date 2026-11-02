/**
 * 功能：ChatView 行为测试（线程回复注入的会话类型口径—— 观察项修复）。
 * 语义：员工线程回复（thread_message 帧）只注入「团队会话 / 项目会话」；
 * 秘书会话（origin=secretary）是通用助理对话，不承载项目员工工作流。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ChatView } from "@/components/chat/ChatView";
import { ApiError } from "@/services/api";
import { companyApi, type ProjectMember } from "@/services/company";
import { sessionsApi, type SessionSummary } from "@/services/sessions";
import { sessionSocket } from "@/services/ws";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

const wsHarness = vi.hoisted(() => ({
  handlers: {} as Record<string, (frame: unknown) => void>,
}));

const abortMock = vi.hoisted(() => vi.fn());

vi.mock("@/services/ws", () => ({
  socket: {
    send: vi.fn(),
    on: vi.fn((event: string, callback: (frame: unknown) => void) => {
      wsHarness.handlers[event] = callback;
      return () => undefined;
    }),
    onFrame: vi.fn(() => () => undefined),
  },
  sessionSocket: {
    send: vi.fn(),
    connect: vi.fn(),
    onFrame: vi.fn(() => () => undefined),
  },
}));

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      employees: vi.fn(),
      projects: vi.fn(),
      workItems: vi.fn(async () => ({ items: [], column_counts: {} })),
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      detail: vi.fn(async () => ({ messages: [] })),
      appendSystemNote: vi.fn(async () => ({ created: true, entry_id: "note-1" })),
      abort: abortMock,
      processSkip: vi.fn(async () => ({ ok: true })),
      regenerate: vi.fn(async () => ({
        session_id: "s1",
        entry_id: "e0",
        reply: "新的回复",
        terminal_reason: "COMPLETED",
      })),
      revertStage: vi.fn(),
      revertCommit: vi.fn(),
      retryTool: vi.fn(async () => ({
        tool_name: "bash",
        success: true,
        output_preview: "fixed",
        error: "",
        duration_ms: 30,
      })),
    },
  };
});

const mocked = vi.mocked(companyApi);

function session(id: string, origin: string): SessionSummary {
  return {
    id,
    title: id,
    created_at: "2026-09-23T01:00:00",
    message_count: 0,
    project_id: "p1",
    ...(origin ? { origin } : {}),
  };
}

function threadFrame(content: string) {
  return {
    type: "thread_message",
    data: {
      thread_id: "th-1",
      sender: "worker",
      content,
      mentions: [],
      message_id: "m-1",
      created_at: "",
      project_id: "p1",
      participants: ["user", "worker"],
    },
  };
}

describe("ChatView 会话类型口径（线程回复注入）", () => {
  beforeEach(() => {
    wsHarness.handlers = {};
    mocked.employees.mockResolvedValue({
      employees: [{ employee_id: "e1", name: "小美", role_id: "worker" }],
    });
    mocked.projects.mockResolvedValue({ projects: [] });
  });

  it("团队会话：员工线程回复实时注入消息流", async () => {
    useChatStore.setState({
      activeSessionId: "s-team",
      activeProjectId: "p1",
      sessions: [session("s-team", "")],
      messagesBySession: { "s-team": [] },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    wsHarness.handlers["thread_message"](threadFrame("员工回复内容"));

    expect(await screen.findByText("员工回复内容")).toBeInTheDocument();
  });

  it("秘书会话：员工线程回复不得注入（通用助理对话不承载项目员工工作流）", async () => {
    useChatStore.setState({
      activeSessionId: "s-secretary",
      activeProjectId: "p1",
      sessions: [session("s-secretary", "secretary")],
      messagesBySession: { "s-secretary": [] },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    wsHarness.handlers["thread_message"](threadFrame("不应出现的员工回复"));

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("不应出现的员工回复")).not.toBeInTheDocument();
  });

  it("秘书会话不渲染员工过程流（LiveProgressList 不出现——员工过程不串入助理会话）", async () => {
    useChatStore.setState({
      activeSessionId: "s-secretary",
      activeProjectId: "p1",
      sessions: [session("s-secretary", "secretary")],
      messagesBySession: { "s-secretary": [] },
      employeeProgress: {
        "p1::th-1::worker": [
          { id: "ep:thought:1", kind: "thought", text: "员工在跑", status: "running" },
        ],
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());
    expect(screen.queryByTestId("live-employee-progress")).not.toBeInTheDocument();
  });

  it("过程固化：员工回复注入时过程块随消息展示并清槽（Day137b-1）", async () => {
    useChatStore.setState({
      activeSessionId: "s-team",
      activeProjectId: "p1",
      sessions: [session("s-team", "")],
      messagesBySession: { "s-team": [] },
      employeeProgress: {
        "p1::th-1::worker": [
          { id: "ep:thought:1", kind: "thought", text: "先分析", status: "success" },
        ],
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    wsHarness.handlers["thread_message"](threadFrame("带过程的回复"));

    expect(await screen.findByText("带过程的回复")).toBeInTheDocument();
    expect(screen.getByTestId("process-block")).toBeInTheDocument();
    expect(useChatStore.getState().employeeProgress["p1::th-1::worker"]).toBeUndefined();
  });

  it("发送 payload 携带当前对话模式（plan，Day137b-1）", async () => {
    useUiStore.setState({ chatMode: "plan" });
    useChatStore.setState({
      activeSessionId: "s-mode",
      activeProjectId: "p1",
      sessions: [session("s-mode", "")],
      messagesBySession: { "s-mode": [] },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "测试模式透传" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(vi.mocked(sessionSocket.send)).toHaveBeenCalledWith(
      expect.objectContaining({ type: "chat", message: "测试模式透传", mode: "plan" }),
    );
    useUiStore.setState({ chatMode: "plan" });
  });

  it("会话详情带 mode 时恢复持久模式（A2）", async () => {
    useUiStore.setState({ chatMode: "execute" });
    useChatStore.setState({
      activeSessionId: "s-persist",
      activeProjectId: "p1",
      sessions: [session("s-persist", "")],
      messagesBySession: { "s-persist": [] },
    });
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: { mode: "plan" },
      messages: [],
    });

    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );

    await waitFor(() => expect(useUiStore.getState().chatMode).toBe("plan"));
  });

  it("停止：abort 404（后端无活动流）→ 本地兜底定稿，防「正在回复中」残留（2026-09-24）", async () => {
    abortMock.mockRejectedValueOnce(new ApiError(404, "NO_ACTIVE_ABORT_CONTROLLER"));
    useChatStore.setState({
      activeSessionId: "s-stop",
      activeProjectId: "p1",
      sessions: [session("s-stop", "")],
      streams: { "s-stop": { sessionId: "s-stop", pendingId: "m1" } },
      messagesBySession: {
        "s-stop": [
          { id: "m1", sessionId: "s-stop", role: "assistant", content: "半截回复", done: false },
        ],
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    fireEvent.click(screen.getByTestId("composer-stop")); // 第一次：确认态
    fireEvent.click(screen.getByTestId("composer-stop")); // 第二次：停止

    await waitFor(() => expect(useChatStore.getState().streams["s-stop"]).toBeUndefined());
    expect(abortMock).toHaveBeenCalledWith("s-stop");
    const pendingMessage = useChatStore
      .getState()
      .messagesBySession["s-stop"]?.find((item) => item.id === "m1");
    expect(pendingMessage?.done).toBe(true);
  });

  it("统一决策区：中断恢复提示与交互卡同 dock，且位于输入框上方（137b-3a）", async () => {
    // detail 加载会把消息流替换为服务端历史——铺一条 user 尾条（done）构成「中断可继续」态
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "user",
          content: "去做事",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-dock",
      sessionTerminals: { "s-dock": "MODEL_ERROR" },
      activeProjectId: "p1",
      sessions: [session("s-dock", "")],
      streams: {},
      messagesBySession: {
        "s-dock": [{ id: "u1", sessionId: "s-dock", role: "user", content: "去做事", done: true }],
      },
      interactions: {
        i1: {
          interaction_id: "i1",
          session_id: "s-dock",
          kind: "confirm",
          payload: { prompt: "允许执行 bash？" },
        },
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    const dock = screen.getByTestId("decision-dock");
    expect(dock).toContainElement(await screen.findByTestId("resume-banner"));
    expect(dock).toContainElement(screen.getByTestId("interaction-card"));
    // 与输入框连体：决策区在同一 Composer 列内（-3 ：边缘对齐/底部贴合）
    const composer = screen.getByTestId("composer");
    expect(composer.contains(dock)).toBe(true);
  });

  it("交互帧切换（interaction_id 变化）→ 卡片状态重建，旧答案不残留（137b-3 key 修复）", async () => {
    const baseFrame = {
      session_id: "s-key",
      kind: "input",
      payload: {
        questions: [
          { id: "q1", question: "甲？" },
          { id: "q2", question: "乙？" },
        ],
      },
    };
    useChatStore.setState({
      activeSessionId: "s-key",
      activeProjectId: "p1",
      sessions: [session("s-key", "")],
      streams: {},
      messagesBySession: {
        "s-key": [{ id: "u1", sessionId: "s-key", role: "user", content: "开工", done: true }],
      },
      interactions: { iA: { ...baseFrame, interaction_id: "iA" } },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    const input = (await screen.findByTestId("decision-input")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "答案甲" } });
    expect(input.value).toBe("答案甲");

    // 帧 A 被 resolve、帧 B 抵达（同会话）——无 key 时 React 复用实例、答案残留
    useChatStore.setState({ interactions: { iB: { ...baseFrame, interaction_id: "iB" } } });
    await waitFor(() => {
      expect((screen.getByTestId("decision-input") as HTMLInputElement).value).toBe("");
    });
  });

  // ①：多张决策卡并存时允许上下切换，且**切走再切回要保留已答内容**
  // （上一条 -3 用例管的是「卡 A 被移除、卡 B 抵达」不得残留；本条管的是「A/B 并存、用户只是切走」）
  it("多张决策卡并存：切换到别的卡再切回，已答内容与分页游标仍在（§18.16①）", async () => {
    const paged = (id: string, prefix: string) => ({
      session_id: "s-multi",
      kind: "input",
      interaction_id: id,
      payload: {
        questions: [
          { id: `${prefix}1`, question: `${prefix}一？` },
          { id: `${prefix}2`, question: `${prefix}二？` },
        ],
      },
    });
    useChatStore.setState({
      activeSessionId: "s-multi",
      activeProjectId: "p1",
      sessions: [session("s-multi", "")],
      streams: {},
      messagesBySession: { "s-multi": [] },
      interactions: { iA: paged("iA", "甲"), iB: paged("iB", "乙") },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );

    // 两张卡并存 → 单一翻页器（‹ n/m ›），起点为卡 1 第 1 题
    expect(await screen.findByTestId("interaction-stack")).toBeInTheDocument();
    expect(screen.getByTestId("decision-next")).toBeEnabled();
    expect(screen.getByTestId("decision-prev")).toBeDisabled();
    expect(screen.getByTestId("decision-page")).toHaveTextContent("1/2");

    // 卡 1：答完第一题，翻到第二题作答（分页游标停在第 2 页）
    fireEvent.change(screen.getByTestId("decision-input"), { target: { value: "甲一答案" } });
    fireEvent.click(screen.getByTestId("decision-next"));
    expect(screen.getByTestId("decision-page")).toHaveTextContent("2/2");
    fireEvent.change(screen.getByTestId("decision-input"), { target: { value: "甲二答案" } });

    // 本卡最后一题再点 › → 切到卡 2（另一决策点），显示其 1/2
    fireEvent.click(screen.getByTestId("decision-next"));
    await waitFor(() => expect(screen.getByTestId("decision-page")).toHaveTextContent("1/2"));
    expect((screen.getByTestId("decision-input") as HTMLInputElement).value).toBe("");

    // 卡 2 首题点 ‹ → 切回卡 1 —— 分页游标与两题答案都必须还在
    fireEvent.click(screen.getByTestId("decision-prev"));
    await waitFor(() => expect(screen.getByTestId("decision-page")).toHaveTextContent("2/2"));
    expect((screen.getByTestId("decision-input") as HTMLInputElement).value).toBe("甲二答案");
  });

  it("跨会话：其他会话流式回复中本会话仍可发送（按会话流槽）", async () => {
    vi.mocked(sessionSocket.send).mockClear();
    useChatStore.setState({
      activeSessionId: "s-a",
      activeProjectId: "p1",
      sessions: [session("s-a", "")],
      streams: { "s-b": { sessionId: "s-b", pendingId: "p-b" } },
      messagesBySession: { "s-a": [] },
      interactions: {},
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    const textbox = await screen.findByRole("textbox");
    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.keyDown(textbox, { key: "Enter" });
    expect(vi.mocked(sessionSocket.send)).toHaveBeenCalled();
  });

  it("中断恢复：点「重试」重发最后一条 user 消息（137b-3 调整）", async () => {
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "user",
          content: "继续汇总",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-retry",
      sessionTerminals: { "s-retry": "MODEL_ERROR" },
      activeProjectId: "p1",
      sessions: [session("s-retry", "")],
      streams: {},
      interactions: {},
      messagesBySession: {
        "s-retry": [
          { id: "u1", sessionId: "s-retry", role: "user", content: "继续汇总", done: true },
        ],
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());
    fireEvent.click(await screen.findByTestId("resume-retry"));
    // 重试现在先终止内核侧旧轮（abort）再重发——发送是异步的（含 1.2s 静置），
    // 用 waitFor 放宽窗口断言（默认 1s 窗口会先于静置结束超时）
    await waitFor(
      () =>
        expect(vi.mocked(sessionSocket.send)).toHaveBeenCalledWith(
          expect.objectContaining({ type: "chat", message: "继续汇总" }),
        ),
      { timeout: 4000 },
    );
  });

  it("中断恢复样式：壳取浅红主题 + 横幅透明融入 + 警示图标 + 黑色重试按钮", async () => {
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "user",
          content: "样式检查",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-style",
      sessionTerminals: { "s-style": "MODEL_ERROR" },
      activeProjectId: "p1",
      sessions: [session("s-style", "")],
      streams: {},
      interactions: {},
      messagesBySession: {
        "s-style": [
          { id: "u1", sessionId: "s-style", role: "user", content: "样式检查", done: true },
        ],
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    const banner = await screen.findByTestId("resume-banner");
    expect(banner.querySelector(".lucide-circle-alert")).toBeTruthy();
    // 红主题贯通：横幅透明融入壳，浅红底由 Composer 壳
    // （aboveTone）承担——与决策卡并发时壳恒红、卡自持白底，不再「红条叠紫底」
    const shell = screen.getByTestId("decision-dock").parentElement;
    expect(shell?.className).toContain("bg-destructive/10");
    expect(screen.getByTestId("resume-retry").className).toContain("bg-chat-ink");
  });

  it("中断恢复：点「取消」本地隐藏提示（不再显示）", async () => {
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "user",
          content: "取消我",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-cancel",
      sessionTerminals: { "s-cancel": "MODEL_ERROR" },
      activeProjectId: "p1",
      sessions: [session("s-cancel", "")],
      streams: {},
      interactions: {},
      messagesBySession: {
        "s-cancel": [
          { id: "u1", sessionId: "s-cancel", role: "user", content: "取消我", done: true },
        ],
      },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());
    fireEvent.click(await screen.findByTestId("resume-cancel"));
    expect(screen.queryByTestId("resume-banner")).not.toBeInTheDocument();
  });
});

describe("ChatView 137b-3b 失败工具节点级操作", () => {
  beforeEach(() => {
    wsHarness.handlers = {};
    vi.clearAllMocks();
    mocked.employees.mockResolvedValue({ employees: [] });
    mocked.projects.mockResolvedValue({ projects: [] });
  });

  /** 功能：detail mock 播种含失败工具过程块的消息（ChatView 挂载即 loadMessages，直写 store 会被覆盖）。 */
  function mockDetailWithFailedTool() {
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        { id: "e0", entry_type: "message", turn_index: 1, role: "user", content: "跑一下" },
        { id: "m1", entry_type: "message", turn_index: 1, role: "assistant", content: "执行完成" },
        {
          id: "p1",
          entry_type: "process",
          turn_index: 1,
          role: "assistant",
          content: "",
          metadata: JSON.stringify({
            process: [
              {
                id: "n1",
                kind: "tool",
                toolName: "bash",
                input: {},
                status: "error",
                detail: "boom",
                durationMs: 5,
                toolCallId: "call_9",
              },
            ],
          }),
        },
      ],
    });
  }

  it("失败工具行不再渲染重试/跳过操作（2026-10-15 用户拍板：子过程行内操作全部移除）", async () => {
    useChatStore.setState({
      activeSessionId: "s-team",
      activeProjectId: "p1",
      sessions: [session("s-team", "")],
      messagesBySession: { "s-team": [] },
    });
    mockDetailWithFailedTool();
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("process-toggle")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    expect(screen.queryByTestId("process-item-retry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("process-item-skip")).not.toBeInTheDocument();
    expect(sessionsApi.retryTool).not.toHaveBeenCalled();
    expect(sessionsApi.processSkip).not.toHaveBeenCalled();
  });
});

describe("ChatView 新建会话空态 + 秘书身份（2026-09-29 用户拍板）", () => {
  beforeEach(() => {
    wsHarness.handlers = {};
    vi.clearAllMocks();
    mocked.employees.mockResolvedValue({ employees: [] });
    mocked.projects.mockResolvedValue({ projects: [] });
  });

  /** 功能：项目列表兜底构造（members = 空数组 → 未拉团队）。 */
  function projectWithMembers(members: ProjectMember[]) {
    return {
      projects: [
        {
          project_id: "p1",
          name: "选品项目",
          root_dir: "",
          team_ids: [],
          created_at: "",
          updated_at: "",
          members,
        },
      ],
    };
  }

  it("已有空会话（无消息、非新建）：与普通空态一致——品牌居中 + 输入框居中", async () => {
    useChatStore.setState({
      activeSessionId: "s-empty",
      activeProjectId: undefined,
      sessions: [],
      streams: {},
      interactions: {},
      messagesBySession: {},
    });
    mocked.employees.mockResolvedValue({ employees: [] });
    mocked.projects.mockResolvedValue({ projects: [] });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );

    const dock = await screen.findByTestId("composer-dock");
    expect(dock.className).toContain("flex-1");
    expect(dock.className).toContain("justify-center");
    const brand = await screen.findByText("bandi");
    expect(brand.className).toContain("text-divider");
  });

  it("空态：品牌字段（Eavm/黑色/放大）+ 秘书提示（浅灰小号）居中布局，不显示秘书头像", async () => {
    useChatStore.setState({
      activeSessionId: null,
      activeProjectId: "p1",
      sessions: [],
      streams: {},
      interactions: {},
      messagesBySession: {},
    });
    mocked.employees.mockResolvedValue({ employees: [] });
    mocked.projects.mockResolvedValue(projectWithMembers([]));
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );

    const brand = await screen.findByText("bandi");
    // 品牌 = 产品名 + 品牌字体（Eavm），浅灰 + 按聊天区宽度等比缩放
    expect(brand.getAttribute("style")).toContain("Eavm");
    expect(brand.getAttribute("style")).toContain("clamp(28px, 7.5cqw, 60px)");
    expect(brand.className).toContain("text-divider");
    expect(brand.parentElement?.getAttribute("style")).toContain("container-type: inline-size");

    const hint = await screen.findByTestId("chat-secretary-hint");
    expect(hint).toHaveTextContent("本项目还没有团队与员工，助理会先接待你");
    // 说明信息：浅灰 + 比现状（text-sm）小；且在「整个会话框 + 项目空间卡」之外（壳外居中）
    expect(hint.className).toContain("text-muted-foreground-weak");
    expect(hint.className).toContain("text-xs");
    expect(hint.closest('[data-testid="composer"]')).toBeNull();
    // 秘书头像不再显示（旧头像 = 独立首字「秘」文本）
    expect(screen.queryByText("秘")).not.toBeInTheDocument();
  });

  it("项目已有团队（成员非空）：只显示品牌，不显示「还没有团队与员工」提示", async () => {
    useChatStore.setState({
      activeSessionId: null,
      activeProjectId: "p1",
      sessions: [],
      streams: {},
      interactions: {},
      messagesBySession: {},
    });
    mocked.projects.mockResolvedValue(
      projectWithMembers([
        {
          team_id: "t1",
          role_id: "dev",
          role_name: "开发",
          employee_id: "e1",
          employee_name: "小美",
          employee_status: "active",
        },
      ]),
    );
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await screen.findByText("bandi");
    await waitFor(() => expect(mocked.projects).toHaveBeenCalled());
    expect(screen.queryByTestId("chat-secretary-hint")).not.toBeInTheDocument();
  });

  it("助理会话：回复身份头为「助理」（2026-10-10 秘书→助理）", async () => {
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "assistant",
          content: "收到，我来处理",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-secretary",
      activeProjectId: "p1",
      sessions: [session("s-secretary", "secretary")],
      streams: {},
      interactions: {},
      messagesBySession: { "s-secretary": [] },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    expect(await screen.findByText("助理")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
  });

  it("无员工（默认仅助理接待）：普通任务会话的回复身份头显示「助理」", async () => {
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "assistant",
          content: "你好，我先帮你处理",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-task",
      activeProjectId: "",
      sessions: [session("s-task", "")],
      streams: {},
      interactions: {},
      messagesBySession: { "s-task": [] },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    expect(await screen.findByText("助理")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
  });

  it("有员工（组织已建）→ 普通会话身份头仍为「助理」", async () => {
    mocked.employees.mockResolvedValue({
      employees: [{ employee_id: "e1", name: "小美", role_id: "dev" }],
    });
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        {
          id: "e1",
          parent_id: null,
          entry_type: "message",
          turn_index: 1,
          role: "assistant",
          content: "通用助理回复",
          tool_call_id: null,
          metadata: "",
          created_at: "",
        },
      ],
    });
    useChatStore.setState({
      activeSessionId: "s-task2",
      activeProjectId: "",
      sessions: [session("s-task2", "")],
      streams: {},
      interactions: {},
      messagesBySession: { "s-task2": [] },
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    expect(await screen.findByText("助理")).toBeInTheDocument();
    expect(screen.queryByText("AI 助手")).not.toBeInTheDocument();
  });

  it("项目解散且无 active 团队：首次发送不发送消息，落持久化提示并锁定输入", async () => {
    vi.mocked(sessionSocket.send).mockClear();
    vi.mocked(sessionsApi.appendSystemNote).mockClear();
    mocked.projects.mockResolvedValue({
      projects: [
        {
          project_id: "p1",
          name: "已解散项目",
          root_dir: "",
          team_ids: [],
          dissolved_team_ids: ["t1"],
          created_at: "",
          updated_at: "",
          members: [],
        },
      ],
    } as never);
    useChatStore.setState({
      activeSessionId: "s-dissolved",
      activeProjectId: "p1",
      sessions: [session("s-dissolved", "")],
      messagesBySession: { "s-dissolved": [] },
      streams: {},
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocked.projects).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "还在吗" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() =>
      expect(vi.mocked(sessionsApi.appendSystemNote)).toHaveBeenCalledWith(
        "s-dissolved",
        expect.any(String),
        "team_dissolved",
        "team_dissolved:t1",
      ),
    );
    expect(vi.mocked(sessionSocket.send)).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("textbox")).toBeDisabled());
  });

  it("群聊 @ 只列 active 项目成员（暂停成员不可选）", async () => {
    mocked.employees.mockResolvedValue({
      employees: [
        { employee_id: "e1", name: "在职员工", role_id: "worker", status: "active" },
        { employee_id: "e2", name: "暂停员工", role_id: "paused", status: "paused" },
      ],
    });
    mocked.projects.mockResolvedValue({
      projects: [
        {
          project_id: "p1",
          name: "承接项目",
          root_dir: "",
          team_ids: ["t1"],
          dissolved_team_ids: [],
          created_at: "",
          updated_at: "",
          members: [
            {
              team_id: "t1",
              role_id: "worker",
              role_name: "在职岗位",
              employee_id: "e1",
              employee_name: "在职员工",
              employee_status: "active",
            },
            {
              team_id: "t1",
              role_id: "paused",
              role_name: "暂停岗位",
              employee_id: "e2",
              employee_name: "暂停员工",
              employee_status: "paused",
            },
          ],
        },
      ],
    } as never);
    useChatStore.setState({
      activeSessionId: "s-mention",
      activeProjectId: "p1",
      sessions: [session("s-mention", "")],
      messagesBySession: { "s-mention": [] },
      streams: {},
    });
    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mocked.projects).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "@" } });
    await screen.findByTestId("composer-mentions");
    expect(await screen.findByTestId("mention-item-employee-worker")).toBeInTheDocument();
    expect(screen.queryByTestId("mention-item-employee-paused")).not.toBeInTheDocument();
  });
});

describe("ChatView 重新生成（A1-B：服务端保留 user/附件）", () => {
  it("调用 sessionsApi.regenerate，不再走 revert + 重发", async () => {
    useChatStore.setState({
      activeSessionId: "s1",
      activeProjectId: "p1",
      sessions: [session("s1", "")],
      messagesBySession: { s1: [] },
    });
    vi.mocked(sessionsApi.detail).mockResolvedValueOnce({
      session: {},
      messages: [
        { id: "e0", entry_type: "message", turn_index: 1, role: "user", content: "问" },
        { id: "m1", entry_type: "message", turn_index: 1, role: "assistant", content: "答" },
      ],
    });
    vi.mocked(sessionsApi.regenerate).mockResolvedValueOnce({
      session_id: "s1",
      entry_id: "e0",
      reply: "新的回复",
      terminal_reason: "COMPLETED",
    });

    render(
      <MemoryRouter>
        <ChatView />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByTestId("message-regenerate"));

    await waitFor(() => expect(sessionsApi.regenerate).toHaveBeenCalledWith("s1", "m1"));
    expect(sessionsApi.revertStage).not.toHaveBeenCalled();
    expect(sessionsApi.revertCommit).not.toHaveBeenCalled();
    expect(sessionSocket.send).not.toHaveBeenCalled();
  });
});
