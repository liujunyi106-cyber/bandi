/**
 * 功能：DmChatView 行为测试（Day137：员工名/头像 + 任务状态徽标 + 空员工引导 +
 * 线程消息过滤（参与方/项目）与切换竞态守卫）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DmChatView } from "@/components/chat/DmChatView";
import { companyApi } from "@/services/company";
import { socket } from "@/services/ws";
import { useChatStore } from "@/stores/chat-store";

const wsHarness = vi.hoisted(() => ({
  handlers: {} as Record<string, (frame: unknown) => void>,
}));

vi.mock("@/services/ws", () => ({
  socket: {
    send: vi.fn(),
    on: vi.fn((event: string, callback: (frame: unknown) => void) => {
      wsHarness.handlers[event] = callback;
      return () => undefined;
    }),
    onFrame: vi.fn(() => () => undefined),
  },
}));

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      employees: vi.fn(),
      workItems: vi.fn(),
      threads: vi.fn(),
      thread: vi.fn(),
      employeeTombstone: vi.fn(),
      appendThreadSystemNote: vi.fn(),
    },
  };
});

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return {
    ...actual,
    sessionsApi: {
      ...actual.sessionsApi,
      companyApprovals: vi.fn().mockResolvedValue({ approvals: [] }),
    },
  };
});

const mocked = vi.mocked(companyApi);
const mockedSocket = vi.mocked(socket);

/** 功能：构造 thread_message 帧（participants 为服务端线程参与方集合）。 */
function threadFrame(overrides: Record<string, unknown>) {
  return {
    type: "thread_message",
    data: {
      thread_id: "th",
      sender: "dev",
      content: "",
      mentions: [],
      message_id: "m",
      created_at: "",
      project_id: "p1",
      participants: ["user", "dev"],
      ...overrides,
    },
  };
}

describe("DmChatView", () => {
  beforeEach(() => {
    wsHarness.handlers = {};
    useChatStore.setState({ employeeProgress: {} });
    mocked.employees.mockResolvedValue({
      employees: [{ employee_id: "e1", name: "小美", role_id: "dev" }],
    });
    mocked.workItems.mockResolvedValue({ items: [], column_counts: {} });
    mocked.threads.mockResolvedValue({ threads: [] });
    mocked.employeeTombstone.mockResolvedValue(null);
    mocked.thread.mockResolvedValue({
      thread_id: "th1",
      parent_task_id: null,
      participants: ["user", "dev"],
      created_at: "",
      messages: [],
    });
  });

  it("显示员工名与任务状态徽标（执行中计数来自本单聊线程的 work-items）", async () => {
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 0,
        },
      ],
    } as never);
    mocked.workItems.mockResolvedValue({
      items: [
        {
          id: "w1",
          title: "做详情页",
          phase: "executing",
          assignee_role: "dev",
          column: "in_progress",
          thread_id: "th-dev",
        },
      ],
      column_counts: {},
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );

    expect(await screen.findByText("小美")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("dm-task-badge")).toHaveTextContent("执行中 · 1"),
    );
  });

  it("无进行中任务时徽标显示空闲", async () => {
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("dm-task-badge")).toHaveTextContent("空闲");
  });

  it("员工执行过程块：thread_progress 归并后实时展示（不依赖任务轮询；Day137b-1 插队批）", async () => {
    useChatStore.setState({
      employeeProgress: {
        "company::::dev": [
          { id: "ep:thought:1", kind: "thought", text: "正在分析需求", status: "running" },
          { id: "ep:tool:2", kind: "tool", toolName: "web_search", status: "running" },
        ],
      },
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId("dm-running")).toBeInTheDocument());
    expect(screen.getByTestId("process-block")).toBeInTheDocument();
  });

  it("群聊线程的过程槽不串入单聊视图（跨线程串台）", async () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th-group::dev": [
          { id: "ep:thought:g", kind: "thought", text: "群聊里的执行", status: "running" },
        ],
      },
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    await screen.findByPlaceholderText("给这位员工发消息…");
    await waitFor(() => expect(mocked.threads).toHaveBeenCalled());

    expect(screen.queryByTestId("dm-running")).not.toBeInTheDocument();
    expect(screen.queryByText("群聊里的执行")).not.toBeInTheDocument();
  });

  it("进行中徽标只统计本单聊线程的卡（其他线程的卡不计入）", async () => {
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 0,
        },
      ],
    } as never);
    mocked.workItems.mockResolvedValue({
      items: [
        {
          id: "w-group",
          title: "群聊任务",
          phase: "executing",
          assignee_role: "dev",
          column: "in_progress",
          thread_id: "th-group",
        },
      ],
      column_counts: {},
    } as never);
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");

    await waitFor(() => expect(screen.getByTestId("dm-task-badge")).toHaveTextContent("空闲"), {
      timeout: 3000,
    });
  });

  it("员工回复到达后「执行中」徽标立即归零（卡终态滞后不得残留）", async () => {
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 0,
        },
      ],
    } as never);
    mocked.workItems.mockResolvedValue({
      items: [
        {
          id: "w1",
          title: "做详情页",
          phase: "executing",
          assignee_role: "dev",
          column: "in_progress",
          thread_id: "th-dev",
        },
      ],
      column_counts: {},
    } as never);
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("dm-task-badge")).toHaveTextContent("执行中 · 1"),
    );

    wsHarness.handlers["thread_message"](
      threadFrame({ thread_id: "th-dev", sender: "dev", content: "已完成", message_id: "m-done" }),
    );

    expect(await screen.findByText("已完成")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("dm-task-badge")).toHaveTextContent("空闲"));
    expect(screen.queryByTestId("dm-running")).not.toBeInTheDocument();
  });

  it("历史线程消息携带 process_events 时（刷新后）仍显示过程块", async () => {
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 1,
        },
      ],
    } as never);
    mocked.thread.mockResolvedValue({
      thread_id: "th-dev",
      parent_task_id: null,
      participants: ["user", "dev"],
      created_at: "",
      messages: [
        {
          sender: "dev",
          content: "历史回复",
          mentions: [],
          created_at: "",
          message_id: "m-hist",
          process_events: [
            { type: "thinking_delta", data: { text: "历史思考" } },
            { type: "done", data: {} },
          ],
        },
      ],
    } as never);

    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );

    expect(await screen.findByText("历史回复")).toBeInTheDocument();
    expect(await screen.findByTestId("process-block")).toBeInTheDocument();
    // 历史快照已收口：不得残留「正在执行…」（消息已到达 = 该轮已结束）
    expect(screen.getByTestId("process-toggle")).toHaveTextContent("已完成");
    expect(screen.getByTestId("process-toggle")).not.toHaveTextContent("正在执行");
    // 历史事件无真实时间戳：不得显示回放瞬间算出的假耗时（如「已完成 · 1ms」）
    expect(screen.getByTestId("process-total-ms").textContent).toBe("");
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(await screen.findByTestId("process-bucket-thought"));
    expect((await screen.findByTestId("process-item-text")).textContent).toContain("历史思考");
  });

  it("消息头与群聊同口径：头像 size-6、姓名 text-sm、底部对齐", async () => {
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 1,
        },
      ],
    } as never);
    mocked.thread.mockResolvedValue({
      thread_id: "th-dev",
      parent_task_id: null,
      participants: ["user", "dev"],
      created_at: "",
      messages: [
        {
          sender: "dev",
          content: "尺寸校验回复",
          mentions: [],
          created_at: "2026-10-24T00:00:00+00:00",
          message_id: "m-size",
        },
      ],
    } as never);

    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );

    expect(await screen.findByText("尺寸校验回复")).toBeInTheDocument();
    const avatar = screen.getByTestId("dm-message-avatar");
    expect(avatar.className).toContain("size-6");
    expect(avatar.className).toContain("rounded-full");
    const meta = screen.getByTestId("dm-message-meta");
    expect(meta.className).toContain("text-sm");
    expect(meta.className).toContain("items-end");
  });

  it("对话框统一：公共 Composer 且模式切换已补齐（plan/execute 透传）", async () => {
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");
    expect(screen.getByTestId("composer")).toBeInTheDocument();
    expect(screen.getByTestId("composer-mode-toggle")).toBeInTheDocument();
  });

  it("过程固化：回复到达时把过程槽附加到该消息（上方过程块）并清槽", async () => {
    useChatStore.setState({
      employeeProgress: {
        "company::th::dev": [
          { id: "ep:thought:1", kind: "thought", text: "先分析需求", status: "success" },
        ],
      },
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await waitFor(() => expect(wsHarness.handlers["thread_message"]).toBeDefined());

    wsHarness.handlers["thread_message"](
      threadFrame({ project_id: "company", content: "员工回复内容", message_id: "m-2" }),
    );

    expect(await screen.findByText("员工回复内容")).toBeInTheDocument();
    expect(screen.getByTestId("process-block")).toBeInTheDocument();
    expect(useChatStore.getState().employeeProgress["company::th::dev"]).toBeUndefined();
  });

  it("无员工时显示空态引导（CTA 而非对话区）", async () => {
    mocked.employees.mockResolvedValue({ employees: [] });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );

    expect(await screen.findByTestId("empty-employee-state")).toBeInTheDocument();
    expect(screen.queryByTestId("dm-chat-view")).not.toBeInTheDocument();
  });

  it("发送消息经 /ws/company（target_role + project_id 透传——线程归属项目空间）", async () => {
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    const input = await screen.findByPlaceholderText("给这位员工发消息…");
    fireEvent.change(input, { target: { value: "帮我整理清单" } });
    fireEvent.click(screen.getByTitle("发送"));

    expect(mockedSocket.send).toHaveBeenCalledWith({
      content: "帮我整理清单",
      sender_role: "user",
      target_role: "dev",
      mode: "execute",
      project_id: "p1",
    });
  });

  it("thread_message 帧按参与方集合精确过滤：本员工直聊注入，群聊/其他员工/跨项目不注入", async () => {
    // 无历史线程 → threadId 未知（原实现的串台窗口）
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    await screen.findByPlaceholderText("给这位员工发消息…");
    await waitFor(() => expect(mocked.threads).toHaveBeenCalled());

    wsHarness.handlers["thread_message"](
      threadFrame({
        thread_id: "th-group",
        sender: "market",
        content: "群聊线程消息",
        participants: ["market"],
      }),
    );
    wsHarness.handlers["thread_message"](
      threadFrame({
        thread_id: "th-other",
        sender: "ops",
        content: "别的员工消息",
        participants: ["user", "ops"],
      }),
    );
    wsHarness.handlers["thread_message"](
      threadFrame({
        thread_id: "th-cross",
        sender: "dev",
        content: "跨项目消息",
        project_id: "p2",
        participants: ["user", "dev"],
      }),
    );
    wsHarness.handlers["thread_message"](
      threadFrame({ thread_id: "th-dev", sender: "dev", content: "本员工回复" }),
    );

    expect(await screen.findByText("本员工回复")).toBeInTheDocument();
    expect(screen.queryByText("群聊线程消息")).not.toBeInTheDocument();
    expect(screen.queryByText("别的员工消息")).not.toBeInTheDocument();
    expect(screen.queryByText("跨项目消息")).not.toBeInTheDocument();
  });

  it("破坏者：@提及在单聊必须高亮为文本主色且可点（与群聊/会话同口径）", async () => {
    // 「@员工，发送信息应显示蓝色」：单聊漏传 mentions ⇒ 既不高亮也不可点
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    await screen.findByPlaceholderText("给这位员工发消息…");
    await waitFor(() => expect(mocked.threads).toHaveBeenCalled());

    wsHarness.handlers["thread_message"](
      threadFrame({ thread_id: "th-dev", sender: "dev", content: "请 @小美 跟进 @dev" }),
    );

    const byName = await screen.findByText("@小美");
    expect(byName.className).toContain("text-mention");
    expect(byName.getAttribute("data-mention-name")).toBe("小美");

    const byRoleId = await screen.findByText("@dev");
    expect(byRoleId.className, "角色 id 形式同样高亮").toContain("text-mention");
  });

  it("引用已通过的任务后发送：payload 带 target_work_item_id，发送后引用清空", async () => {
    mocked.workItems.mockResolvedValue({
      items: [
        { id: "w1", title: "核对价格带", phase: "approved", assignee_role: "dev" },
        { id: "w2", title: "进行中任务", phase: "running", assignee_role: "dev" },
      ],
      column_counts: {},
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    await screen.findByPlaceholderText("给这位员工发消息…");

    fireEvent.click(await screen.findByTestId("dm-rework-picker"));
    expect(await screen.findByText("核对价格带")).toBeInTheDocument();
    expect(screen.queryByText("进行中任务")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("dm-rework-option"));
    expect(screen.getByTestId("dm-rework-chip")).toHaveTextContent("核对价格带");

    const input = screen.getByPlaceholderText("给这位员工发消息…");
    fireEvent.change(input, { target: { value: "价格带改成 89-129" } });
    fireEvent.click(screen.getByTitle("发送"));

    expect(mockedSocket.send).toHaveBeenCalledWith({
      content: "价格带改成 89-129",
      sender_role: "user",
      target_role: "dev",
      mode: "execute",
      project_id: "p1",
      target_work_item_id: "w1",
    });
    expect(screen.queryByTestId("dm-rework-chip")).not.toBeInTheDocument();
  });

  it("未引用任务时发送 payload 不带 target_work_item_id（回归护栏）", async () => {
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    const input = await screen.findByPlaceholderText("给这位员工发消息…");
    fireEvent.change(input, { target: { value: "普通消息" } });
    fireEvent.click(screen.getByTitle("发送"));

    expect(mockedSocket.send).toHaveBeenCalledWith({
      content: "普通消息",
      sender_role: "user",
      target_role: "dev",
      mode: "execute",
      project_id: "p1",
    });
  });

  it("切换 targetRole 后旧线程响应迟到不得覆盖新视图（竞态守卫）", async () => {
    let release: () => void = () => undefined;
    const firstGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let calls = 0;
    mocked.threads.mockImplementation(async () => {
      calls += 1;
      if (calls === 1) {
        await firstGate;
        return {
          threads: [
            {
              thread_id: "th-old",
              parent_task_id: null,
              participants: ["user", "ops"],
              created_at: "",
              message_count: 1,
            },
          ],
        };
      }
      return { threads: [] };
    });
    mocked.thread.mockResolvedValue({
      thread_id: "th-old",
      parent_task_id: null,
      participants: ["user", "ops"],
      created_at: "",
      messages: [
        {
          sender: "ops",
          content: "旧员工迟到消息",
          mentions: [],
          created_at: "",
          message_id: "m-old",
        },
      ],
    });

    const { rerender } = render(
      <MemoryRouter>
        <DmChatView targetRole="ops" projectId="p1" />
      </MemoryRouter>,
    );
    rerender(
      <MemoryRouter>
        <DmChatView targetRole="dev" projectId="p1" />
      </MemoryRouter>,
    );
    release();

    await waitFor(() => expect(calls).toBe(2));
    await waitFor(() => expect(screen.queryByText("旧员工迟到消息")).not.toBeInTheDocument());
  });
});

/**
 * 功能：单聊必须真的有「模型控制条」这个 UI 入口。
 *
 * 证据：`DmChatView` 早已把 `employeeModel` 一路传到 `Composer`，但没传 `enhanced`
 * 而 `Composer` 的控制条是 `{enhanced ? <ComposerControls/> : …}` ⇒ 单聊视图**根本没有**
 * 模型控件，锁定态无从显示（本仓库 「传了 ≠ 有入口」同一类）。
 * 下面钉的是「渲染出了控件」这个行为，而不是「某 prop 被传了」。
 */
describe("DmChatView 单聊控制条与 ⊕ 菜单（2026-09-30 用户拍板）", () => {
  beforeEach(() => {
    mocked.employees.mockResolvedValue({
      employees: [
        {
          employee_id: "e1",
          name: "小美",
          role_id: "dev",
          llm_config: { default_model: "deepseek-v4.1-flash" },
        },
      ],
    });
  });

  it("渲染控制条并显示该员工实际使用的模型（§18.15② 有 UI 入口）", async () => {
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");
    const field = await screen.findByTestId("composer-model");
    expect(field).toHaveTextContent("deepseek-v4.1-flash");
  });

  it("⊕ 菜单：保留上传与@引用文件，不提供「选择团队」（单聊无团队上下文）", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");
    await user.click(await screen.findByTestId("composer-plus"));
    expect(screen.getByTestId("composer-plus-attach")).toBeInTheDocument();
    expect(screen.getByTestId("composer-plus-quote")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-plus-team")).not.toBeInTheDocument();
  });

  it("员工未配独立模型时控制条不显示锁定模型（回落全局，不误锁）", async () => {
    mocked.employees.mockResolvedValue({
      employees: [{ employee_id: "e1", name: "小美", role_id: "dev" }],
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");
    await screen.findByTestId("composer-model");
    expect(screen.queryByText(/deepseek-v4\.1-flash/)).not.toBeInTheDocument();
  });

  it("所属员工权限卡在单聊内渲染（不再只显示执行中、无法决策）", async () => {
    useChatStore.setState({
      interactions: {
        "i-dm-perm": {
          interaction_id: "i-dm-perm",
          session_id: "role-session::coffee-shop::dev::dev",
          kind: "permission",
          payload: {
            ui_variant: "permission",
            message: "需要你的确认：bash echo DM_TIMING",
            tool_name: "bash",
          },
        },
      } as never,
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");
    expect(screen.getByTestId("dm-interaction-stack")).toBeInTheDocument();
    expect(screen.getByTestId("permission-allow-once")).toBeInTheDocument();
  });

  it("needs_attention 卡虽在 in_progress 列但不再计入「执行中」", async () => {
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th-dev",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 0,
        },
      ],
    } as never);
    mocked.workItems.mockResolvedValue({
      items: [
        {
          id: "w2",
          title: "需要人工",
          phase: "needs_attention",
          assignee_role: "dev",
          column: "in_progress",
          thread_id: "th-dev",
          created_at: "2026-10-25 06:49:32.000+00:00",
        },
      ],
      column_counts: {},
    });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("dm-task-badge")).toHaveTextContent("空闲");
  });

  it("已删除员工：首次发送不发送消息，落持久化退役提示并锁定输入", async () => {
    mockedSocket.send.mockClear();
    mocked.employeeTombstone.mockResolvedValue({
      tombstone: { employee_id: "e1", role_id: "dev", name: "小美" },
    } as never);
    mocked.threads.mockResolvedValue({
      threads: [
        {
          thread_id: "th1",
          parent_task_id: null,
          participants: ["user", "dev"],
          created_at: "",
          message_count: 0,
        },
      ],
    } as never);
    mocked.appendThreadSystemNote.mockResolvedValue({ created: true, thread_id: "th1" });
    render(
      <MemoryRouter>
        <DmChatView targetRole="dev" />
      </MemoryRouter>,
    );
    await screen.findByText("小美");
    await waitFor(() => expect(mocked.threads).toHaveBeenCalled());

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "还在吗" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    await waitFor(() =>
      expect(mocked.appendThreadSystemNote).toHaveBeenCalledWith(
        "th1",
        expect.any(String),
        "employee_retired",
        "",
        "employee_retired:dev",
      ),
    );
    expect(mockedSocket.send).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("textbox")).toBeDisabled());
  });
});
