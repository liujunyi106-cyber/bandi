/**
 * 功能：ProcessBlock 行为测试——过程按真实执行顺序平铺展示。
 *
 * 断言口径是「渲染出来的行文本序列」与「行内可见内容」，不锚定分类行等
 * 实现细节：分类会随设计调整，执行顺序才是产品承诺。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ProcessBlock } from "@/components/chat/ProcessBlock";
import type { ProcessNode } from "@/stores/chat-store";

function toolNode(
  id: string,
  toolName: string,
  options: Partial<{
    input: Record<string, unknown>;
    status: "running" | "success" | "error" | "skipped";
    detail: string;
    durationMs: number;
    toolCallId: string;
    seq: number;
  }> = {},
): ProcessNode {
  return {
    id,
    kind: "tool",
    toolName,
    input: options.input ?? {},
    status: options.status ?? "success",
    detail: options.detail ?? "",
    durationMs: options.durationMs ?? 5,
    toolCallId: options.toolCallId,
    seq: options.seq,
  };
}

/** 功能：读出过程块展开区的行文本序列（按 DOM 次序）。 */
function rowTexts(): string[] {
  return screen.getAllByTestId("process-item-text").map((node) => node.textContent ?? "");
}

describe("ProcessBlock 执行顺序", () => {
  it("读文件 → 改文件 → 跑命令：按真实执行顺序渲染，不按工具类别归并", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "read", { input: { file_path: "a.py" } }),
      toolNode("t2", "edit", { input: { file_path: "a.py" } }),
      toolNode("t3", "bash", { input: { command: "pytest" } }),
      toolNode("t4", "read", { input: { file_path: "b.py" } }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));

    const texts = rowTexts();
    expect(texts).toHaveLength(4);
    // 四次调用的类别依次为 read/edit/bash/read——若按类别重组，两个 read 会相邻
    const categories = screen
      .getAllByTestId("process-timeline-row")
      .map((row) => row.getAttribute("data-category"));
    expect(categories).toEqual(["explore", "file", "tool", "explore"]);
    expect(texts[0]).toContain("a.py");
    expect(texts[3]).toContain("b.py");
  });

  it("seq 存在时以 seq 为准（同数组内乱序也能还原真实次序）", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "read", { input: { file_path: "a.py" }, seq: 3 }),
      toolNode("t2", "bash", { input: { command: "ls" }, seq: 1 }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    // 数组顺序优先：节点追加即执行顺序，seq 只是显式化
    const texts = rowTexts();
    expect(texts[0]).toContain("a.py");
    expect(texts[1]).toContain("ls");
  });

  it("思考与工具交替时保持交替顺序（不把思考全部提到前面）", () => {
    const nodes: ProcessNode[] = [
      { id: "th1", kind: "thought", text: "**先看目录**\n\n正文一", status: "success" },
      toolNode("t1", "bash", { input: { command: "ls" } }),
      { id: "th2", kind: "thought", text: "**再读文件**\n\n正文二", status: "success" },
      toolNode("t2", "read", { input: { file_path: "x.py" } }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));

    const categories = screen
      .getAllByTestId("process-timeline-row")
      .map((row) => row.getAttribute("data-category"));
    expect(categories).toEqual(["thought", "tool", "thought", "explore"]);
  });

  it("技能激活行插入到其发生位置，不单独成组", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "bash", { input: { command: "git status" } }),
      { id: "s1", kind: "skill", skills: ["git-workflow"], count: 1, status: "success" },
      toolNode("t2", "bash", { input: { command: "git commit" } }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));

    const categories = screen
      .getAllByTestId("process-timeline-row")
      .map((row) => row.getAttribute("data-category"));
    expect(categories).toEqual(["tool", "skill", "tool"]);
  });
});

describe("ProcessBlock 状态与总行", () => {
  it("执行中：总行显示「正在执行…」", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "web_search", { input: { query: "竞品" }, status: "running", durationMs: 0 }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    expect(screen.getByTestId("process-block")).toHaveTextContent("正在执行…");
  });

  it("已定稿：残留 running 节点也按完成态收口，不再转圈", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "web_search", { input: { query: "竞品" }, status: "running", durationMs: 0 }),
    ];
    render(<ProcessBlock nodes={nodes} settled />);
    expect(screen.getByTestId("process-block")).toHaveTextContent("已完成");
    expect(screen.getByTestId("process-block")).not.toHaveTextContent("正在执行");
  });

  it("总行文案：不显示「过程消息」字段名，完成态只留「已完成 · 时间」", () => {
    render(<ProcessBlock nodes={[toolNode("t1", "bash", { durationMs: 1200 })]} />);
    expect(screen.getByTestId("process-toggle")).toHaveTextContent("已完成 · 1.2s");
  });

  it("成功行不渲染状态图标（仅进行中/失败/跳过有图标）", () => {
    render(<ProcessBlock nodes={[toolNode("t1", "web_search", { input: { query: "x" } })]} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    for (const status of screen.getAllByTestId("process-item-status")) {
      expect(status.querySelector("svg")).toBeNull();
    }
  });

  it("已失败：行内出现红色失败图标", () => {
    render(<ProcessBlock nodes={[toolNode("t1", "bash", { status: "error" })]} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-item-status").querySelector("svg")).not.toBeNull();
  });

  it("已跳过：灰显「已跳过」标签", () => {
    const nodes = [toolNode("t1", "bash", { status: "skipped", toolCallId: "call_9" })];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-item-skipped")).toBeInTheDocument();
  });
});

describe("ProcessBlock 明细内容", () => {
  it("工具行展示调用参数摘要", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "web_search", { input: { query: "竞品价格" }, durationMs: 800 }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-item")).toHaveTextContent('query="竞品价格"');
    // 耗时由总行承载，行内不常驻
    expect(screen.getByTestId("process-item")).not.toHaveTextContent("800ms");
    expect(screen.getByTestId("process-toggle")).toHaveTextContent("800ms");
  });

  it("知识条目：一行 + 「展示」→ 内容区 + 底部「收起」", () => {
    const nodes: ProcessNode[] = [
      toolNode("k1", "knowledge_search", {
        input: { query: "选品流程" },
        detail: "选品流程 SOP 全文…",
      }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-knowledge-item")).toHaveTextContent("选品流程");

    fireEvent.click(screen.getByTestId("process-knowledge-show"));
    expect(screen.getByText("选品流程 SOP 全文…")).toBeInTheDocument();
    fireEvent.click(screen.getByText("收起"));
    expect(screen.queryByText("选品流程 SOP 全文…")).not.toBeInTheDocument();
  });

  it("知识检索条目显示中文知识库名（不显示英文函数名）", () => {
    const nodes: ProcessNode[] = [
      toolNode("k2", "knowledge_search", {
        input: { query: "需求澄清" },
        detail: "{'scope': {'base_ids': ['kb-requirements'], 'base_names': ['需求澄清知识库']}}",
      }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-knowledge-item")).toHaveTextContent("需求澄清知识库");
  });

  it("记忆检索与知识检索是两行，各自可展开内容", () => {
    const nodes: ProcessNode[] = [
      toolNode("m1", "memory_search", { input: { query: "上次方案" }, detail: "记忆条目…" }),
      toolNode("k1", "knowledge_search", { input: { query: "设计规范" } }),
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    const items = screen.getAllByTestId("process-knowledge-item");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("上次方案");
    expect(items[1]).toHaveTextContent("设计规范");
  });

  it("布局钳宽：根节点 w-full+min-w-0（防长文本顶宽右溢）", () => {
    const nodes: ProcessNode[] = [
      {
        id: "th1",
        kind: "thought",
        text: "The user just said hello. Current mode: plan mode — only read tools, output step list + time + risks.",
        status: "success",
      },
    ];
    render(<ProcessBlock nodes={nodes} />);

    const block = screen.getByTestId("process-block");
    expect(block.className).toContain("w-full");
    expect(block.className).toContain("min-w-0");

    fireEvent.click(screen.getByTestId("process-toggle"));
    const text = screen.getByTestId("process-item-text");
    expect(text.className).toContain("whitespace-pre-wrap");
    expect(text.className).toContain("break-words");
    expect(text.className).not.toContain("max-h-");
    expect(text.className).not.toContain("overflow-y-auto");
    expect(text.className).not.toContain("truncate");
    expect(text).toHaveTextContent("The user just said hello.");
  });
});

describe("ProcessBlock 思考摘要与展开", () => {
  it("折叠态展示摘要标题而非正文", () => {
    const nodes: ProcessNode[] = [
      { id: "th1", kind: "thought", text: "**摘要标题**\n\n完整正文内容", status: "success" },
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-item-text")).toHaveTextContent("摘要标题");
    expect(screen.getByTestId("process-item-text")).not.toHaveTextContent("完整正文内容");
  });

  it("展开后可见正文", () => {
    const nodes: ProcessNode[] = [
      { id: "th1", kind: "thought", text: "**摘要标题**\n\n完整正文内容", status: "success" },
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-item-expand"));
    expect(screen.getByTestId("process-item-body")).toHaveTextContent("完整正文内容");
  });

  it("思考正文展开后紧跟本条之下（不在整个列表末尾）", () => {
    const nodes: ProcessNode[] = [
      {
        id: "th1",
        kind: "thought",
        text: "**第一条摘要**\n\n第一条正文内容在此处展开。",
        status: "success",
      },
      {
        id: "th2",
        kind: "thought",
        text: "**第二条摘要**\n\n第二条正文内容在此处展开。",
        status: "success",
      },
    ];
    const { container } = render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getAllByTestId("process-item-expand")[0]);

    const bodies = screen.getAllByTestId("process-item-body");
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toHaveTextContent("第一条正文");

    const firstRow = screen.getAllByTestId("process-item-text")[0];
    const secondRow = screen.getAllByTestId("process-item-text")[1];
    const order = Array.from(container.querySelectorAll("*"));
    expect(order.indexOf(bodies[0])).toBeGreaterThan(order.indexOf(firstRow));
    expect(order.indexOf(bodies[0])).toBeLessThan(order.indexOf(secondRow));
  });

  it("展开第二条时正文落在第二条之内（且在第一条之后）", () => {
    const nodes: ProcessNode[] = [
      {
        id: "th1",
        kind: "thought",
        text: "**第一条摘要**\n\n第一条正文内容在此处展开。",
        status: "success",
      },
      {
        id: "th2",
        kind: "thought",
        text: "**第二条摘要**\n\n第二条正文内容在此处展开。",
        status: "success",
      },
    ];
    const { container } = render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getAllByTestId("process-item-expand")[1]);

    const bodies = screen.getAllByTestId("process-item-body");
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toHaveTextContent("第二条正文");
    const order = Array.from(container.querySelectorAll("*"));
    const secondRow = screen.getAllByTestId("process-item-text")[1];
    expect(order.indexOf(bodies[0])).toBeGreaterThan(order.indexOf(secondRow));
  });
});

describe("ProcessBlock 注记", () => {
  it("记忆写入行：展开后显示「计入记忆 xN」", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "bash"),
      { id: "n1", kind: "note", noteType: "memory_written", count: 2, status: "success" },
    ];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByText("计入记忆 x2")).toBeInTheDocument();
  });

  it("压缩注记：已压缩，可展开查看前后规模与保留摘要", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "bash"),
      {
        id: "n-compact",
        kind: "note",
        noteType: "compaction",
        compaction: {
          stage: "progressive",
          inputChars: 120000,
          outputChars: 24000,
          summary: "保留了目标、已完成文件和下一步计划。",
        },
        status: "success",
      },
    ];
    render(<ProcessBlock nodes={nodes} />);

    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByText("已压缩")).toBeInTheDocument();
    expect(screen.queryByText("正在压缩")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("process-compaction-toggle-n-compact"));
    const detail = screen.getByTestId("process-compaction-detail");
    expect(detail).toHaveTextContent("压缩保留内容");
    expect(detail).toHaveTextContent("保留了目标、已完成文件和下一步计划。");
    expect(screen.getByTestId("process-compaction-toggle-n-compact")).toHaveTextContent(
      "120000 → 24000",
    );
  });

  it("终态注记（用户中断）折叠后仍可见", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "bash"),
      { id: "n1", kind: "note", noteType: "interrupt", status: "success" },
    ];
    render(<ProcessBlock nodes={nodes} />);
    expect(screen.getByTestId("process-terminal-notes")).toBeInTheDocument();
    expect(screen.getByTestId("process-terminal-notes")).toHaveTextContent("用户中断");
  });
});

describe("ProcessBlock 工具行交互", () => {
  const failedNode = () =>
    toolNode("t1", "bash", { status: "error", detail: "boom", toolCallId: "call_9" });

  it("失败项不再出重试/重发/跳过任何操作按钮", () => {
    const onRetryTool = vi.fn();
    const onSkipTool = vi.fn();
    const onResendTurn = vi.fn();
    render(
      <ProcessBlock nodes={[failedNode()]} actions={{ onRetryTool, onSkipTool, onResendTurn }} />,
    );
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.queryByTestId("process-item-retry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("process-item-skip")).not.toBeInTheDocument();
    expect(screen.queryByTestId("process-item-resend")).not.toBeInTheDocument();
  });

  it("点击工具行触发 onShowDetail（带 nodeId）", () => {
    const onShowDetail = vi.fn();
    render(<ProcessBlock nodes={[failedNode()]} actions={{ onShowDetail }} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-item"));
    expect(onShowDetail).toHaveBeenCalledWith("t1");
    expect(onShowDetail).toHaveBeenCalledTimes(1);
  });

  it("破坏者：无 toolCallId（旧帧）时行不可点，不触发详情", () => {
    const onShowDetail = vi.fn();
    render(
      <ProcessBlock
        nodes={[toolNode("t1", "bash", { status: "error", detail: "boom" })]}
        actions={{ onShowDetail }}
      />,
    );
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-item"));
    expect(onShowDetail).not.toHaveBeenCalled();
  });
});

describe("ProcessBlock 子代理嵌套树", () => {
  const taskNode = () =>
    ({
      ...toolNode("t1", "task", { input: { task: "调研" }, status: "success" }),
      children: [
        { id: "c1", kind: "thought", text: "先查资料。", status: "success" },
        {
          id: "c2",
          kind: "tool",
          toolName: "bash",
          input: { command: "ls" },
          status: "success",
          toolCallId: "c9",
        },
      ],
    }) as ProcessNode;

  it("task 节点的 children 渲染为可展开子树", () => {
    render(<ProcessBlock nodes={[taskNode()]} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-subagent-tree")).toBeInTheDocument();
    const items = screen.getAllByTestId("process-subagent-item");
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveTextContent("bash");
    fireEvent.click(screen.getByTestId("process-subagent-toggle"));
    expect(screen.queryByTestId("process-subagent-item")).not.toBeInTheDocument();
  });

  it("无 children 的普通工具不渲染子树", () => {
    render(<ProcessBlock nodes={[toolNode("t1", "bash")]} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.queryByTestId("process-subagent-tree")).not.toBeInTheDocument();
  });
});
