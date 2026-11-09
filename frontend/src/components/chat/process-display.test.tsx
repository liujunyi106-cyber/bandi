/**
 * 契约：过程展示的度量与分级（标本对照）。
 *
 * 标本要求（用户提供的真实会话）：
 * 「思考完成」+ 一句话转述摘要（≠ 原始 CoT）
 * 「2次设计准则查阅」「7次资料搜索」（同类聚合计数）
 * 「原型设计团队 正在为你工作」（员工身份头 + 进行中）
 * 「已完结 · 0.0s」必须显示**真实耗时**
 *
 * 钉行为不钉实现：断言渲染结果，不断言组件结构。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ChatMessage, ProcessNode } from "@/stores/chat-store";
import { applyThinking, applyToolStart, finalizeMessage } from "@/stores/chat-store";
import { MessageItem } from "./MessageItem";
import { ProcessBlock } from "./ProcessBlock";
import {
  thinkingSummary,
  aggregateToolNodes,
  formatDurationMs,
  totalDurationMs,
} from "./process-display";

function emptyMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    sessionId: "s1",
    role: "assistant",
    content: "",
    done: false,
    ...overrides,
  };
}

describe("thinkingSummary（思考摘要提取，规则非 LLM）", () => {
  it("markdown 首个标题作为摘要", () => {
    expect(thinkingSummary("## 先确认项目结构\n\n接下来读取配置")).toEqual({
      title: "先确认项目结构",
      body: "接下来读取配置",
    });
  });

  it("加粗首行作为摘要（opencode 口径）", () => {
    expect(thinkingSummary("**Inspecting PR workflow**\n\n正文内容")).toEqual({
      title: "Inspecting PR workflow",
      body: "正文内容",
    });
  });

  it("无标题时摘要回落到首句并截断", () => {
    const { title } = thinkingSummary(
      "用户希望构建一个城市漫游网站并要求参考图片素材。后面还有很多解释。",
    );
    expect(title).toBe("用户希望构建一个城市漫游网站并要求参考图片素材。");
    expect(title.length).toBeLessThanOrEqual(40);
  });

  it("空思考返回空标题且不抛", () => {
    expect(thinkingSummary("")).toEqual({ title: "", body: "" });
  });

  it("纯空白思考不产生标题", () => {
    expect(thinkingSummary("   \n\n  ").title).toBe("");
  });
});

describe("aggregateToolNodes（同类聚合计数，opencode ContextToolGroup 口径）", () => {
  const tool = (toolName: string, status: "success" | "error" = "success"): ProcessNode => ({
    id: `t-${toolName}-${Math.random()}`,
    kind: "tool",
    toolName,
    status,
  });

  it("同工具多次调用合并计数", () => {
    const groups = aggregateToolNodes([tool("web_search"), tool("web_search"), tool("web_search")]);
    expect(groups).toHaveLength(1);
    expect(groups[0].count).toBe(3);
    expect(groups[0].toolName).toBe("web_search");
  });

  it("不同工具分列", () => {
    const groups = aggregateToolNodes([tool("read"), tool("grep"), tool("read")]);
    expect(groups.map((g) => g.toolName)).toEqual(["read", "grep"]);
    expect(groups.find((g) => g.toolName === "read")?.count).toBe(2);
  });

  it("保留首次出现顺序（不打乱时序）", () => {
    const groups = aggregateToolNodes([tool("b"), tool("a"), tool("b")]);
    expect(groups.map((g) => g.toolName)).toEqual(["b", "a"]);
  });

  it("组内任一失败则组标记含错误", () => {
    const groups = aggregateToolNodes([tool("read"), tool("read", "error")]);
    expect(groups[0].hasError).toBe(true);
  });

  it("运行中的调用计入组且标记运行中", () => {
    const groups = aggregateToolNodes([
      { id: "x", kind: "tool", toolName: "read", status: "running" },
    ]);
    expect(groups[0].running).toBe(true);
    expect(groups[0].count).toBe(1);
  });
});

describe("ProcessBlock 度量：真实耗时（修 0.0s）", () => {
  it("纯思考轮显示思考节点耗时，不再是 0.0s", () => {
    const nodes: ProcessNode[] = [
      { id: "n1", kind: "thought", text: "先确认结构", status: "success", durationMs: 4200 },
    ];
    render(<ProcessBlock nodes={nodes} />);
    expect(screen.getByTestId("process-total-ms")).toHaveTextContent("4.2s");
  });

  it("思考 + 工具混合时耗时累加两类", () => {
    const nodes: ProcessNode[] = [
      { id: "n1", kind: "thought", text: "想", status: "success", durationMs: 1500 },
      { id: "n2", kind: "tool", toolName: "read", status: "success", durationMs: 500 },
    ];
    render(<ProcessBlock nodes={nodes} />);
    expect(screen.getByTestId("process-total-ms")).toHaveTextContent("2.0s");
  });

  it("无任何度量时不显示时间（避免误导性的「· 0ms」）", () => {
    const nodes: ProcessNode[] = [{ id: "n1", kind: "tool", toolName: "read", status: "success" }];
    render(<ProcessBlock nodes={nodes} />);
    expect(screen.getByTestId("process-total-ms")).toHaveTextContent("");
    // 总行也不带「· 」分隔（无时间则不出现分隔符）
    expect(screen.getByTestId("process-toggle")).toHaveTextContent("已完成");
  });
});

describe("数据侧接线：事件 → 节点 durationMs（防假接线）", () => {
  /**
   * 变异验证发现：仅测展示层时，`settleThought` 恒返回 0 仍全绿 ——
   * 即「显示逻辑对、但数据从没接上」。此处用**假时钟**钉住真实耗时值
   * （断言必须 > 0 且等于推进的时长，否则 0 也能蒙混过关）。
   */
  const T0 = 1_700_000_000_000;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("思考节点在切换到工具时结算出真实耗时", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "先确认结构");
    vi.setSystemTime(T0 + 4200);
    list = applyToolStart(list, "p1", "read");
    const node = list[0].process?.[0];
    if (node?.kind !== "thought") throw new Error("首节点应为思考节点");
    expect(node.status).toBe("success");
    expect(node.durationMs).toBe(4200);
    // 起始时间戳必须被清掉（否则节点体积随时间线增长）
    expect(node.startedAt).toBeUndefined();
  });

  it("finalize 后思考节点带耗时（turn 结束路径）", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "想一想");
    vi.setSystemTime(T0 + 1500);
    list = finalizeMessage(list, "p1", {});
    const node = list[0].process?.[0];
    if (node?.kind !== "thought") throw new Error("首节点应为思考节点");
    expect(node.durationMs).toBe(1500);
  });

  it("纯思考轮（无工具）也有耗时 —— 这是 0.0s bug 的直接回归守卫", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "只有思考没有工具");
    vi.setSystemTime(T0 + 7300);
    list = finalizeMessage(list, "p1", {});
    const nodes = list[0].process ?? [];
    expect(nodes.length).toBe(1);
    expect(totalDurationMs(nodes)).toBe(7300);
    expect(formatDurationMs(totalDurationMs(nodes))).toBe("7.3s");
  });

  it("运行中的工具节点不被提前结算（工具确实还在跑）", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyToolStart(list, "p1", "web_fetch", { url: "https://x" });
    list = applyToolStart(list, "p1", "read");
    const first = list[0].process?.[0];
    expect(first?.status).toBe("running");
  });
});

describe("ProcessBlock 桶头聚合（标本「资料搜索 ×7」）", () => {
  const many = (toolName: string, n: number): ProcessNode[] =>
    Array.from({ length: n }, (_, i) => ({
      id: `${toolName}-${i}`,
      kind: "tool" as const,
      toolName,
      status: "success" as const,
    }));

  function openBlock(container: HTMLElement) {
    fireEvent.click(container.querySelector('[data-testid="process-toggle"]') as HTMLElement);
  }

  it("同类多次只显示「工具名 ×N」", () => {
    const { container } = render(<ProcessBlock nodes={many("web_search", 7)} />);
    openBlock(container);
    const head = screen.getByTestId("process-bucket-count-web");
    expect(head).toHaveTextContent("联网搜索 ×7");
  });

  it("同桶多工具并列计数（搜索+抓取）", () => {
    const { container } = render(
      <ProcessBlock nodes={[...many("web_search", 3), ...many("web_fetch", 2)]} />,
    );
    openBlock(container);
    const head = screen.getByTestId("process-bucket-count-web");
    expect(head).toHaveTextContent("联网搜索 ×3");
    expect(head).toHaveTextContent("网页读取 ×2");
  });

  it("单次调用回落到 x1（不显示冗余工具名）", () => {
    const { container } = render(<ProcessBlock nodes={many("web_search", 1)} />);
    openBlock(container);
    expect(screen.getByTestId("process-bucket-count-web")).toHaveTextContent("x1");
  });
});

describe("终态注记：折叠后仍可见（spec ⑥ 用户中断 / 断线）", () => {
  const withNote = (noteType: string): ProcessNode[] => [
    { id: "n1", kind: "thought", text: "想", status: "success", durationMs: 100 },
    { id: "n2", kind: "note", noteType, status: "success" },
  ];

  it("用户中断字段在过程块折叠态可见", () => {
    const { container } = render(<ProcessBlock nodes={withNote("interrupt")} />);
    // 不展开过程块：终态注记也必须渲染
    expect(container.querySelector('[data-testid="process-terminal-notes"]')).not.toBeNull();
    expect(screen.getByTestId("process-note-terminal")).toBeInTheDocument();
  });

  it("断线注记同样在折叠态可见", () => {
    render(<ProcessBlock nodes={withNote("disconnected")} />);
    expect(screen.getByTestId("process-note-terminal")).toBeInTheDocument();
  });

  it("过程内注记（记入记忆）仍留在展开区内", () => {
    const { container } = render(<ProcessBlock nodes={withNote("memory_written")} />);
    expect(container.querySelector('[data-testid="process-terminal-notes"]')).toBeNull();
    // 折叠态不可见（在展开区内）
    expect(screen.queryByTestId("process-note")).not.toBeInTheDocument();
  });
});

describe("终态消息不得再挂 running 节点（回合后过程块不结算）", () => {
  it("已终态消息收到晚到 tool_call_start ⇒ 节点直接结算（不卡 running）", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "想");
    list = finalizeMessage(list, "p1", {});
    // 终态之后工具帧才到（竞态：done 帧先到、工具帧后到）
    list = applyToolStart(list, "p1", "list_dir");
    const nodes = list[0].process ?? [];
    const tool = nodes.find((n) => n.kind === "tool");
    expect(tool?.status).toBe("success");
    // 关键：过程块据此判定 running=false ⇒ 耗时可见 + 自动折叠
    expect(nodes.some((n) => n.status === "running")).toBe(false);
  });

  it("未终态消息的 tool_call_start 仍为 running（不误伤实时态）", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyToolStart(list, "p1", "list_dir");
    const tool = (list[0].process ?? []).find((n) => n.kind === "tool");
    expect(tool?.status).toBe("running");
  });

  it("序列复现：思考→定稿→晚到工具 ⇒ 总耗时可见且非空", () => {
    let list = [emptyMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "## 摘要\n\n正文");
    vi.setSystemTime(Date.now() + 3000);
    list = finalizeMessage(list, "p1", {});
    list = applyToolStart(list, "p1", "glob");
    list = applyToolStart(list, "p1", "list_dir");
    const nodes = list[0].process ?? [];
    expect(totalDurationMs(nodes)).not.toBeNull();
    expect(formatDurationMs(totalDurationMs(nodes))).toBe("3.0s");
  });
});

describe("spec ④：重试无果弹出重试卡片", () => {
  it("error 存在时渲染重试卡片与重试按钮（不再只是红字）", () => {
    const onResendTurn = vi.fn();
    render(
      <MessageItem
        message={{
          id: "m1",
          sessionId: "s1",
          role: "assistant",
          content: "",
          done: true,
          error: "LLM stream ended without completion (incomplete)",
        }}
        onResendTurn={onResendTurn}
      />,
    );
    expect(screen.getByTestId("message-retry-card")).toBeInTheDocument();
    const btn = screen.getByTestId("message-retry-button");
    fireEvent.click(btn);
    expect(onResendTurn).toHaveBeenCalledTimes(1);
  });

  it("无 error 时不渲染重试卡片", () => {
    render(
      <MessageItem
        message={{ id: "m1", sessionId: "s1", role: "assistant", content: "好", done: true }}
      />,
    );
    expect(screen.queryByTestId("message-retry-card")).not.toBeInTheDocument();
  });
});

describe("思考条目不显示逐条耗时（避免挤占展开按钮）", () => {
  function openThought(container: HTMLElement) {
    fireEvent.click(container.querySelector('[data-testid="process-toggle"]') as HTMLElement);
    fireEvent.click(
      container.querySelector('[data-testid="process-bucket-toggle-thought"]') as HTMLElement,
    );
  }

  it("思考条目：有展开按钮但无逐条 ms（耗时由总行承载）", () => {
    const nodes: ProcessNode[] = [
      {
        id: "n1",
        kind: "thought",
        text: "## 摘要\n\n正文",
        status: "success",
        durationMs: 42125,
      },
    ];
    const { container } = render(<ProcessBlock nodes={nodes} />);
    openThought(container);
    expect(screen.getByTestId("process-item-expand")).toBeInTheDocument();
    expect(screen.queryByText("42125ms")).not.toBeInTheDocument();
    // 总行仍显示总耗时
    expect(screen.getByTestId("process-total-ms")).toHaveTextContent("42.1s");
  });

  it("工具条目：行内不显示逐条耗时，只在总行汇总", () => {
    // 2026-10-12 毫秒级耗时对用户无信息量且挤占展开按钮位置。
    // 破坏者视角：把耗时加回明细行时本用例必须变红。
    const nodes: ProcessNode[] = [
      { id: "t1", kind: "tool", toolName: "web_search", status: "success", durationMs: 320 },
    ];
    const { container } = render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(container.querySelector('[data-testid="process-toggle"]') as HTMLElement);
    fireEvent.click(
      container.querySelector('[data-testid="process-bucket-toggle-web"]') as HTMLElement,
    );
    // 总行汇总仍保留耗时（唯一一处）
    expect(screen.getAllByText("320ms")).toHaveLength(1);
  });
});

describe("ProcessBlock 分级：摘要可见、正文需展开", () => {
  const nodes: ProcessNode[] = [
    {
      id: "n1",
      kind: "thought",
      text: "## 先看目录结构\n\n这里是很长的思考正文，不该默认占屏。",
      status: "success",
    },
  ];

  /** 过程块外层默认折叠；思考摘要位于「思考」桶内，需先展开外层与该桶。 */
  function openThoughtBucket(container: HTMLElement) {
    fireEvent.click(container.querySelector('[data-testid="process-toggle"]') as HTMLElement);
    fireEvent.click(
      container.querySelector('[data-testid="process-bucket-toggle-thought"]') as HTMLElement,
    );
  }

  it("折叠态展示摘要标题而非正文", () => {
    const { container } = render(<ProcessBlock nodes={nodes} />);
    openThoughtBucket(container);
    expect(screen.getByText("先看目录结构")).toBeInTheDocument();
    expect(screen.queryByTestId("process-item-body")).not.toBeInTheDocument();
  });

  it("展开后可见正文", () => {
    const { container } = render(<ProcessBlock nodes={nodes} />);
    openThoughtBucket(container);
    fireEvent.click(screen.getByTestId("process-item-expand"));
    expect(screen.getByTestId("process-item-body")).toHaveTextContent(/这里是很长的思考正文/);
  });
});
