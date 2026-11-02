/**
 * 功能：ProcessBlock 行为测试（v5 原型形态：分类折叠 / 知识展示 / 执行状态）。
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
  };
}

describe("ProcessBlock", () => {
  it("执行中：总行显示「正在执行…」，分类行无折叠按钮", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "web_search", { input: { query: "竞品" }, status: "running", durationMs: 0 }),
      toolNode("t2", "web_search", { input: { query: "价格" }, status: "running", durationMs: 0 }),
    ];
    render(<ProcessBlock nodes={nodes} />);

    expect(screen.getByTestId("process-block")).toHaveTextContent("正在执行…");
    expect(screen.getByTestId("process-bucket-web")).toHaveTextContent("联网搜索");
    // 多次调用时桶头显示「中文工具名 ×N」（不暴露英文函数名）
    // 单次调用仍回落 x1；计数语义不变，只补上工具名。
    expect(screen.getByTestId("process-bucket-count-web")).toHaveTextContent("联网搜索 ×2");
    expect(screen.queryByTestId("process-bucket-toggle-web")).not.toBeInTheDocument();
  });

  it("已完成：总行「已完成」；展开分类 → 展开明细（搜索词；行尾不再常驻耗时）", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "web_search", { input: { query: "竞品价格" }, durationMs: 800 }),
    ];
    render(<ProcessBlock nodes={nodes} />);

    expect(screen.getByTestId("process-block")).toHaveTextContent("已完成");
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-web"));
    const item = screen.getByTestId("process-item");
    expect(item).toHaveTextContent('query="竞品价格"');
    // 2026-10-12 用户拍板：明细行右侧不再常驻耗时（12ms/800ms）——
    // 该位置本应承载展开/收起，而工具行的展开入口是点击行打开右侧详情。
    expect(item).not.toHaveTextContent("800ms");
    // 耗时仍由总行汇总呈现
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
    fireEvent.click(screen.getByTestId("process-bucket-knowledge"));
    expect(screen.getByTestId("process-knowledge-item")).toHaveTextContent("选品流程");

    fireEvent.click(screen.getByTestId("process-knowledge-show"));
    expect(screen.getByText("选品流程 SOP 全文…")).toBeInTheDocument();
    fireEvent.click(screen.getByText("收起"));
    expect(screen.queryByText("选品流程 SOP 全文…")).not.toBeInTheDocument();
  });

  it("记忆检索：单独「调用记忆」桶，不与知识库混桶", () => {
    const nodes: ProcessNode[] = [
      toolNode("m1", "memory_search", { input: { query: "上次方案" }, detail: "记忆条目…" }),
      toolNode("k1", "knowledge_search", { input: { query: "设计规范" } }),
    ];
    render(<ProcessBlock nodes={nodes} />);

    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByTestId("process-bucket-memory")).toHaveTextContent("调用记忆");
    expect(screen.getByTestId("process-bucket-knowledge")).toHaveTextContent("调用知识库");
    fireEvent.click(screen.getByTestId("process-bucket-memory"));
    expect(screen.getByTestId("process-knowledge-item")).toHaveTextContent("上次方案");
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
    fireEvent.click(screen.getByTestId("process-bucket-knowledge"));
    expect(screen.getByTestId("process-knowledge-item")).toHaveTextContent("需求澄清知识库");
  });

  it("成功执行的明细行不再渲染勾号（仅进行中/失败/跳过有状态图标）", () => {
    const nodes: ProcessNode[] = [toolNode("t1", "web_search", { input: { query: "x" } })];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-web"));
    const statuses = screen.getAllByTestId("process-item-status");
    for (const status of statuses) {
      expect(status.querySelector("svg")).toBeNull();
    }
  });

  it("Skills 分类：×N 计数与展开后技能名", () => {
    const nodes: ProcessNode[] = [
      {
        id: "s1",
        kind: "skill",
        skills: ["git-workflow", "code-review"],
        count: 2,
        status: "success",
      },
    ];
    render(<ProcessBlock nodes={nodes} />);

    fireEvent.click(screen.getByTestId("process-toggle"));
    const bucket = screen.getByTestId("process-bucket-skill");
    expect(bucket).toHaveTextContent("调用 Skills");
    expect(bucket).toHaveTextContent("x2");
    fireEvent.click(bucket);
    expect(screen.getByText("git-workflow")).toBeInTheDocument();
    expect(screen.getByText("code-review")).toBeInTheDocument();
  });

  it("记忆写入行：展开后显示「计入记忆 xN」（按写入通道条数）", () => {
    const nodes: ProcessNode[] = [
      toolNode("t1", "bash"),
      { id: "n1", kind: "note", noteType: "memory_written", count: 2, status: "success" },
    ];
    render(<ProcessBlock nodes={nodes} />);

    fireEvent.click(screen.getByTestId("process-toggle"));
    expect(screen.getByText("计入记忆 x2")).toBeInTheDocument();
  });

  it("布局钳宽：根节点 w-full+min-w-0（父层 items-start 下防长文本把 fit-content 顶宽右溢）", () => {
    const nodes: ProcessNode[] = [
      {
        id: "th1",
        kind: "thought",
        text: "The user just said hello. Current mode: plan mode — only read tools, output step list + time + risks. But it's just a greeting. Respond briefly in Chinese.",
        status: "success",
      },
    ];
    render(<ProcessBlock nodes={nodes} />);

    // 根：w-full 撑满消息列；min-w-0 解除 flex 项目最小尺寸下限（两者缺一即右溢卡片）
    const block = screen.getByTestId("process-block");
    expect(block.className).toContain("w-full");
    expect(block.className).toContain("min-w-0");

    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-thought"));

    // 明细文本：多行（上限 3 行）+ 纵向滚动，不再单行省略
    const text = screen.getByTestId("process-item-text");
    expect(text.className).toContain("overflow-y-auto");
    expect(text.className).toContain("whitespace-pre-wrap");
    expect(text.className).toContain("break-words");
    expect(text.className).toContain("max-h-[58.5px]");
    expect(text.className).not.toContain("truncate");
  });

it("总行文案：不再显示「过程消息」字段名，完成态只留「已完成 · 时间」", () => {
    render(<ProcessBlock nodes={[toolNode("t1", "bash", { durationMs: 1200 })]} />);

    const toggle = screen.getByTestId("process-toggle");
    expect(toggle).toHaveTextContent("已完成 · 1.2s");
  });

  it("思考正文展开后紧跟本条之下（不在整个列表末尾）", () => {
    // thinkingSummary 口径：**标题**\n\n正文 → 摘要进行内、正文进 detail 展开区
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
    fireEvent.click(screen.getByTestId("process-bucket-thought"));

    // 只展开第一条
    fireEvent.click(screen.getAllByTestId("process-item-expand")[0]);

    const bodies = screen.getAllByTestId("process-item-body");
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toHaveTextContent("第一条正文");

    // 关键断言：正文容器必须位于「第一条摘要」与「第二条摘要」之间——
    // 展开第 1 条时，正文不得落到第 2 条后面（旧实现恒在列表末尾）
    const firstRow = screen.getAllByTestId("process-item-text")[0];
    const secondRow = screen.getAllByTestId("process-item-text")[1];
    const order = Array.from(container.querySelectorAll("*"));
    const bodyIndex = order.indexOf(bodies[0]);
    expect(bodyIndex).toBeGreaterThan(order.indexOf(firstRow));
    expect(bodyIndex).toBeLessThan(order.indexOf(secondRow));
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
    fireEvent.click(screen.getByTestId("process-bucket-thought"));
    fireEvent.click(screen.getAllByTestId("process-item-expand")[1]);

    const bodies = screen.getAllByTestId("process-item-body");
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toHaveTextContent("第二条正文");
    const order = Array.from(container.querySelectorAll("*"));
    const secondRow = screen.getAllByTestId("process-item-text")[1];
    expect(order.indexOf(bodies[0])).toBeGreaterThan(order.indexOf(secondRow));
  });
});

describe("ProcessBlock 失败工具行（2026-10-15 行内操作移除）", () => {
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
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    expect(screen.queryByTestId("process-item-retry")).not.toBeInTheDocument();
    expect(screen.queryByTestId("process-item-skip")).not.toBeInTheDocument();
    expect(screen.queryByTestId("process-item-resend")).not.toBeInTheDocument();
  });

  it("分类行无悬浮灰态（2026-10-15：hover 背景移除，箭头仍可旋转点击）", () => {
    render(<ProcessBlock nodes={[failedNode()]} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    const row = screen.getByTestId("process-bucket-tool");
    expect(row.className).not.toContain("hover:bg-primary/5");
  });

  it("已跳过：灰显「已跳过」标签", () => {
    const nodes = [toolNode("t1", "bash", { status: "skipped", toolCallId: "call_9" })];
    render(<ProcessBlock nodes={nodes} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    expect(screen.getByTestId("process-item-skipped")).toBeInTheDocument();
  });
});

describe("ProcessBlock v5 G4 工具明细行 → 详情", () => {
  it("点击工具明细行触发 onShowDetail（带 nodeId）", () => {
    const onShowDetail = vi.fn();
    const onRetryTool = vi.fn();
    render(
      <ProcessBlock
        nodes={[toolNode("t1", "bash", { status: "error", detail: "boom", toolCallId: "call_9" })]}
        actions={{ onShowDetail, onRetryTool }}
      />,
    );
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
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
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    fireEvent.click(screen.getByTestId("process-item"));
    expect(onShowDetail).not.toHaveBeenCalled();
  });
});

describe("ProcessBlock 子代理嵌套树（AUD4-5）", () => {
  it("task 节点的 children 渲染为可展开子树", () => {
    const base = toolNode("t1", "task", { input: { task: "调研" }, status: "success" });
    const task = {
      ...base,
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
    } as ProcessNode;
    render(<ProcessBlock nodes={[task]} />);
    fireEvent.click(screen.getByTestId("process-toggle"));
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
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
    fireEvent.click(screen.getByTestId("process-bucket-tool"));
    expect(screen.queryByTestId("process-subagent-tree")).not.toBeInTheDocument();
  });
});
