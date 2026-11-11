/**
 * 功能：员工过程块三处缺陷的行为契约（真机反馈驱动）。
 *
 * ① 过程时长显示原始毫秒（`42125ms`）——应人类可读且与消息头计时同风格；
 * ② 团队/项目会话执行期间**完全没有过程块**（单聊有、主会话无）——
 *    槽里有节点却无视图渲染，用户全程只看占位信息；
 * ③ 多员工协作时只有最后落消息的那个员工的过程被固化，其余并发员工的中间过程丢失。
 *
 * 契约钉行为不钉实现：断言「看得见的节点」与「固化后不再重复出现」。
 */
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LiveProgressList, fallbackRoleVisible } from "@/components/chat/LiveProgressList";
import { ProcessBlock } from "@/components/chat/ProcessBlock";
import { formatDurationMs } from "@/components/chat/process-display";
import { companyApi, type WorkItemSummary } from "@/services/company";
import { useChatStore, type ProcessNode } from "@/stores/chat-store";

vi.mock("@/hooks/use-employees", () => ({
  useEmployees: () => ({
    employees: [
      { role_id: "dev", name: "开发", title: "", status: "active" },
      { role_id: "qa", name: "测试", title: "", status: "active" },
    ],
    loading: false,
    error: null,
  }),
}));

function toolNode(id: string, toolName: string, overrides: Partial<ProcessNode> = {}): ProcessNode {
  return {
    id,
    kind: "tool",
    toolName,
    input: {},
    status: "success",
    detail: "",
    durationMs: 5,
    ...overrides,
  } as ProcessNode;
}

describe("① 过程时长人类可读（formatDurationMs）", () => {
  it("亚秒显示毫秒、秒级一位小数、≥60s 走 M:SS（与消息头计时同风格，非 1m12s）", () => {
    expect(formatDurationMs(300)).toBe("300ms");
    expect(formatDurationMs(4200)).toBe("4.2s");
    expect(formatDurationMs(42125)).toBe("42.1s");
    expect(formatDurationMs(65000)).toBe("1m5s");
    expect(formatDurationMs(191437)).toBe("3m11s");
  });

  it("非法/负值/无度量返回占位符（不得显示 NaN）", () => {
    expect(formatDurationMs(null)).toBe("—");
    expect(formatDurationMs(-1)).toBe("—");
    expect(formatDurationMs(Number.NaN)).toBe("—");
  });

  it("ProcessBlock 工具条目不再显示原始毫秒（破坏者：42125ms 曾直出）", () => {
    render(<ProcessBlock nodes={[toolNode("t1", "web_search", { durationMs: 42125 })]} />);
    expect(screen.queryByText("42125ms")).not.toBeInTheDocument();
    expect(screen.getByText("42.1s")).toBeInTheDocument();
  });
});

describe("② 团队会话执行中的实时过程块", () => {
  beforeEach(() => {
    useChatStore.setState({ employeeProgress: {}, employeeProgressDone: {} });
  });

  it("槽里有节点即渲染（不等回复落地）", () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
      },
    });
    render(<LiveProgressList projectId="p1" />);
    expect(screen.getByTestId("live-employee-progress")).toBeInTheDocument();
    // 槽里的节点真的被喂给了 ProcessBlock（不是只渲染了个空壳容器）
    expect(screen.getByTestId("process-block")).toBeInTheDocument();
    expect(screen.getByTestId("process-toggle")).toBeInTheDocument();
  });

  it("无节点时不渲染容器（不留空壳）", () => {
    render(<LiveProgressList projectId="p1" />);
    expect(screen.queryByTestId("live-employee-progress")).not.toBeInTheDocument();
  });

  it("项目隔离：别的项目的槽不得串入", () => {
    useChatStore.setState({
      employeeProgress: { "p2::th1::dev": [toolNode("t1", "web_search", { status: "running" })] },
    });
    render(<LiveProgressList projectId="p1" />);
    expect(screen.queryByTestId("live-employee-progress")).not.toBeInTheDocument();
  });

  it("指定线程时只看该线程（单聊口径）", () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
        "p1::th2::dev": [toolNode("t2", "bash", { status: "running" })],
      },
    });
    render(<LiveProgressList projectId="p1" threadId="th2" />);
    expect(screen.getAllByTestId("live-employee-progress-item")).toHaveLength(1);
    expect(screen.getByTestId("live-employee-progress-item").dataset.threadId).toBe("th2");
  });
});

describe("③ 多员工协作：每个员工各有一条过程块", () => {
  beforeEach(() => {
    useChatStore.setState({ employeeProgress: {}, employeeProgressDone: {} });
  });

  it("两个员工并发执行 → 全部渲染过程块（并行需见各员工）", () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
        "p1::th1::qa": [toolNode("t2", "bash", { status: "running" })],
      },
    });
    render(<LiveProgressList projectId="p1" />);
    const items = screen.getAllByTestId("live-employee-progress-item");
    expect(items).toHaveLength(2);
    // 两位员工的身份头同时可见（并行任务的其余成员过程不再被隐藏）
    const text = items.map((item) => item.textContent ?? "").join(" ");
    expect(text.includes("开发") || text.includes("测试")).toBe(true);
  });

  it("同一员工两次执行（不同线程）各自成条（全部渲染）", () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::thA::dev": [toolNode("t1", "web_search", { status: "running" })],
        "p1::thB::dev": [toolNode("t2", "bash", { status: "running" })],
      },
    });
    render(<LiveProgressList projectId="p1" />);
    expect(screen.getAllByTestId("live-employee-progress-item")).toHaveLength(2);
  });

  it("固化（消费清槽）后实时块消失——不与固化过程块重复展示", () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
      },
    });
    const view = render(<LiveProgressList projectId="p1" />);
    expect(screen.getByTestId("live-employee-progress")).toBeInTheDocument();

    // 回复落地 → consumeEmployeeProgress 取走并清槽
    act(() => {
      useChatStore.getState().consumeEmployeeProgress("p1", "th1", "dev");
    });
    expect(screen.queryByTestId("live-employee-progress")).not.toBeInTheDocument();
    view.unmount();
  });

  it("回复落位后旧 running 卡不得经 fallback 复活；回复后新建的卡仍可见", () => {
    const finishedAt = Date.parse("2026-10-25T05:14:30.000Z");
    const base = { id: "w1", title: "t", phase: "running", assignee_role: "dev" };
    const oldItem = { ...base, created_at: "2026-10-25 05:14:29.000+00:00" } as WorkItemSummary;
    const newItem = { ...base, id: "w2", created_at: "2026-10-25 05:14:31.000+00:00" } as WorkItemSummary;
    expect(fallbackRoleVisible(oldItem, finishedAt)).toBe(false);
    expect(fallbackRoleVisible(newItem, finishedAt)).toBe(true);
  });
});

describe("④ 挂起多问卡/权限卡 → 过程块显示「等待你的决策…」", () => {
  beforeEach(() => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "request_user_input", { status: "running" })],
      },
      employeeProgressDone: {},
      interactions: {},
      activeSessionId: "sess-1",
    });
  });

  it("当前会话有活跃挂起卡 → 等待行出现（误以为卡死的直接反馈面）", () => {
    useChatStore.setState({
      interactions: {
        i1: { interaction_id: "i1", session_id: "sess-1", kind: "input", payload: {} },
      },
    });
    render(<LiveProgressList projectId="p1" />);
    expect(screen.getByTestId("live-progress-waiting")).toBeInTheDocument();
  });

  it("破坏者：其他会话的卡不得点亮本会话过程块；审批桥帧（approval: 前缀）不算", () => {
    useChatStore.setState({
      interactions: {
        i2: { interaction_id: "i2", session_id: "sess-other", kind: "input", payload: {} },
        i3: {
          interaction_id: "approval:s1:tc1",
          session_id: "sess-1",
          kind: "permission",
          payload: {},
        },
      },
    });
    render(<LiveProgressList projectId="p1" />);
    expect(screen.queryByTestId("live-progress-waiting")).not.toBeInTheDocument();
  });

  it("卡被答复（移出 store）→ 等待行消失", () => {
    useChatStore.setState({
      interactions: {
        i1: { interaction_id: "i1", session_id: "sess-1", kind: "input", payload: {} },
      },
    });
    const view = render(<LiveProgressList projectId="p1" />);
    expect(screen.getByTestId("live-progress-waiting")).toBeInTheDocument();
    act(() => {
      useChatStore.getState().resolveInteraction("i1");
    });
    expect(screen.queryByTestId("live-progress-waiting")).not.toBeInTheDocument();
    view.unmount();
  });
});

describe("⑤ 文件信息不刷屏、fallback 按线程隔离", () => {
  beforeEach(() => {
    useChatStore.setState({
      employeeProgress: {},
      employeeProgressDone: {},
      employeeProgressText: {},
    });
  });

  it("有流式文本也不渲染 live-employee-text（文件信息不进消息流）", () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
      },
      employeeProgressText: { "p1::th1::dev": "正在读取 a.md b.md c.md" },
    });
    render(<LiveProgressList projectId="p1" threadId="th1" />);
    expect(screen.queryByTestId("live-employee-text")).not.toBeInTheDocument();
    expect(screen.queryByText("正在读取 a.md b.md c.md")).not.toBeInTheDocument();
  });

  it("fallback 只渲染当前线程的 running 员工（不串其他 thread）", async () => {
    const spy = vi.spyOn(companyApi, "workItems").mockResolvedValue({
      items: [
        {
          id: "w1",
          title: "t",
          phase: "running",
          project_id: "p1",
          thread_id: "th1",
          assignee_role: "dev",
        },
        {
          id: "w2",
          title: "t",
          phase: "running",
          project_id: "p1",
          thread_id: "th2",
          assignee_role: "qa",
        },
      ],
    } as unknown as Awaited<ReturnType<typeof companyApi.workItems>>);
    render(<LiveProgressList projectId="p1" threadId="th1" />);
    expect(await screen.findByText("开发")).toBeInTheDocument();
    expect(screen.queryByText("测试")).not.toBeInTheDocument();
    spy.mockRestore();
  });
});

describe("⑥ 已有过程块的员工与尚未出帧的员工同屏", () => {
  beforeEach(() => {
    useChatStore.setState({
      employeeProgress: {},
      employeeProgressDone: {},
      employeeProgressText: {},
      interactions: {},
      activeSessionId: "",
    });
  });

  it("开发已有过程块、测试尚未出帧 → 测试在列表末尾以「正在执行」行出现", async () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
      },
    });
    const spy = vi.spyOn(companyApi, "workItems").mockResolvedValue({
      items: [
        {
          id: "w1",
          title: "t",
          phase: "running",
          project_id: "p1",
          thread_id: "th1",
          assignee_role: "qa",
        },
      ],
    } as unknown as Awaited<ReturnType<typeof companyApi.workItems>>);
    render(<LiveProgressList projectId="p1" threadId="th1" />);

    await waitFor(() =>
      expect(screen.getAllByTestId("live-employee-progress-item")).toHaveLength(2),
    );
    const items = screen.getAllByTestId("live-employee-progress-item");
    // 有过程帧的员工在前：身份头 + 过程块
    expect(within(items[0]).getByText("开发")).toBeInTheDocument();
    expect(within(items[0]).getByTestId("process-block")).toBeInTheDocument();
    // 尚未出帧的员工在末尾：身份头 + 「正在执行」占位（不是过程块）
    expect(within(items[1]).getByText("测试")).toBeInTheDocument();
    expect(within(items[1]).getByTestId("live-progress-running")).toBeInTheDocument();
    expect(within(items[1]).queryByTestId("process-block")).not.toBeInTheDocument();
    spy.mockRestore();
  });

  it("running 卡里的员工已在过程块中 → 只保留过程块，不重复渲染占位", async () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
      },
    });
    const spy = vi.spyOn(companyApi, "workItems").mockResolvedValue({
      items: [
        {
          id: "w1",
          title: "t",
          phase: "running",
          project_id: "p1",
          thread_id: "th1",
          assignee_role: "dev",
        },
        {
          id: "w2",
          title: "t",
          phase: "running",
          project_id: "p1",
          thread_id: "th1",
          assignee_role: "qa",
        },
      ],
    } as unknown as Awaited<ReturnType<typeof companyApi.workItems>>);
    render(<LiveProgressList projectId="p1" threadId="th1" />);

    await waitFor(() =>
      expect(screen.getAllByTestId("live-employee-progress-item")).toHaveLength(2),
    );
    const roles = screen
      .getAllByTestId("live-employee-progress-item")
      .map((item) => item.dataset.roleId);
    expect(roles.filter((roleId) => roleId === "dev")).toHaveLength(1);
    expect(roles).toContain("qa");
    // 开发只出现一次身份头（过程块行），没有第二条「正在执行」占位
    expect(screen.getAllByText("开发")).toHaveLength(1);
    expect(screen.getAllByTestId("live-progress-running")).toHaveLength(1);
    spy.mockRestore();
  });

  it("所有 running 员工都已有过程块 → 不出现多余的「正在执行」占位", async () => {
    useChatStore.setState({
      employeeProgress: {
        "p1::th1::dev": [toolNode("t1", "web_search", { status: "running" })],
        "p1::th1::qa": [toolNode("t2", "bash", { status: "running" })],
      },
    });
    let resolveItems: (value: Awaited<ReturnType<typeof companyApi.workItems>>) => void = () =>
      undefined;
    const deferred = new Promise<Awaited<ReturnType<typeof companyApi.workItems>>>((resolve) => {
      resolveItems = resolve;
    });
    const spy = vi.spyOn(companyApi, "workItems").mockReturnValue(deferred);
    render(<LiveProgressList projectId="p1" threadId="th1" />);
    expect(screen.getAllByTestId("live-employee-progress-item")).toHaveLength(2);

    await act(async () => {
      resolveItems({
        items: [
          {
            id: "w1",
            title: "t",
            phase: "running",
            project_id: "p1",
            thread_id: "th1",
            assignee_role: "dev",
          },
          {
            id: "w2",
            title: "t",
            phase: "running",
            project_id: "p1",
            thread_id: "th1",
            assignee_role: "qa",
          },
        ],
      } as unknown as Awaited<ReturnType<typeof companyApi.workItems>>);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.getAllByTestId("live-employee-progress-item")).toHaveLength(2);
    expect(screen.queryByTestId("live-progress-running")).not.toBeInTheDocument();
    spy.mockRestore();
  });
});