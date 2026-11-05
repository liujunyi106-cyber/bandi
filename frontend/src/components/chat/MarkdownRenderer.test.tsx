/**
 * 功能：MarkdownRenderer 行为测试（Markdown 渲染 / XSS 净化 / mermaid 块 / @提及高亮）。
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import { highlightMentions, splitMentions, splitMermaidBlocks } from "@/lib/markdown";

const mermaidMock = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn(async () => ({ svg: "<svg data-mermaid='1'></svg>" })),
}));

vi.mock("mermaid", () => ({ default: mermaidMock }));

describe("splitMermaidBlocks", () => {
  it("无围栏时单个 markdown 段", () => {
    const result = splitMermaidBlocks("普通文本");
    expect(result).toEqual([{ type: "markdown", content: "普通文本" }]);
  });

  it("单围栏切成 markdown + mermaid 两段", () => {
    const result = splitMermaidBlocks("前文\n```mermaid\ngraph TD;A-->B\n```\n后文");
    expect(result).toHaveLength(3);
    expect(result[0]).toEqual({ type: "markdown", content: "前文\n" });
    expect(result[1]).toEqual({ type: "mermaid", code: "graph TD;A-->B" });
    expect(result[2]).toEqual({ type: "markdown", content: "\n后文" });
  });

  it("语言标记大小写不敏感（Mermaid / MERMAID）", () => {
    expect(splitMermaidBlocks("```Mermaid\nx\n```")[0]).toEqual({ type: "mermaid", code: "x" });
    expect(splitMermaidBlocks("```MERMAID\nx\n```")[0]).toEqual({ type: "mermaid", code: "x" });
  });

  it("语言行带图表类型（```mermaid flowchart LR）——类型并入代码（B6 真机容错）", () => {
    const result = splitMermaidBlocks("```mermaid flowchart LR\nA-->B\n```");
    expect(result[0]).toEqual({ type: "mermaid", code: "flowchart LR\nA-->B" });
  });

  it("裸围栏残渣段跳过（真机灰条根因：```mermaid 前后残留 ``` 不渲染空 pre）", () => {
    const result = splitMermaidBlocks("```\n```mermaid\nA-->B\n```\n```");
    expect(result).toEqual([{ type: "mermaid", code: "A-->B" }]);
  });

  it("非 mermaid 围栏不切分", () => {
    const result = splitMermaidBlocks("```python\nprint(1)\n```");
    expect(result).toHaveLength(1);
    expect(result[0].type).toBe("markdown");
  });
});

describe("MarkdownRenderer", () => {
  it("Markdown 结构渲染为标签（标题 / 粗体 / 列表）", () => {
    const { container } = render(<MarkdownRenderer text={"## 小标题\n\n**加粗**\n\n- 一\n- 二"} />);
    expect(container.querySelector("h2")?.textContent).toBe("小标题");
    expect(container.querySelector("strong")?.textContent).toBe("加粗");
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("破坏者：script/onerror 等危险内容被净化", () => {
    const { container } = render(
      <MarkdownRenderer
        text={'<img src="x" onerror="window.__xss=1"><script>window.__xss=2</script>'}
      />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.innerHTML).not.toContain("onerror");
  });

  it("SVG/MathML style 元素与 form 动作不得进入 DOM", () => {
    const { container } = render(
      <MarkdownRenderer
        text={
          '<svg><style>*{position:fixed;inset:0}</style></svg>' +
          "<math><mtext><style>@import 'https://evil.example/a.css';</style></mtext></math>" +
          '<form action="https://evil.example/phish"><input type="submit" /></form>'
        }
      />,
    );
    expect(container.querySelector("style")).toBeNull();
    expect(container.querySelector("svg")).toBeNull();
    expect(container.querySelector("math")).toBeNull();
    expect(container.querySelector("form")).toBeNull();
    expect(container.innerHTML).not.toContain("evil.example");
  });

  it("链接统一新窗口打开（target/rel）", () => {
    const { container } = render(<MarkdownRenderer text={"[示例](https://example.com)"} />);
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.com");
    expect(link?.getAttribute("target")).toBe("_blank");
    expect(link?.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("代码块保留（渲染为 pre/code）", () => {
    const { container } = render(<MarkdownRenderer text={"```python\nprint(1)\n```"} />);
    expect(container.querySelector("pre code")?.textContent).toContain("print(1)");
  });

  it("@提及高亮命中员工名（主色 span）", () => {
    const { container } = render(
      <MarkdownRenderer text={"@市场调研 请看下"} mentions={["市场调研"]} />,
    );
    const marked = container.querySelector(".text-mention");
    expect(marked?.textContent).toBe("@市场调研");
  });

  it("破坏者：代码块内的 @提及不得高亮", () => {
    const { container } = render(
      <MarkdownRenderer text={"正文 @市场调研\n\n```\n@市场调研\n```"} mentions={["市场调研"]} />,
    );
    const highlights = container.querySelectorAll(".text-mention");
    expect(highlights).toHaveLength(1);
    expect(container.querySelector("pre code")?.textContent).toContain("@市场调研");
  });

  it("mermaid 块渲染为 SVG（动态加载成功路径）", async () => {
    const { container } = render(<MarkdownRenderer text={"```mermaid\ngraph TD;A-->B\n```"} />);
    await waitFor(() => expect(container.querySelector("svg")).toBeTruthy());
  });

  it("mermaid 渲染失败回落原始代码（渐进增强）", async () => {
    mermaidMock.render.mockRejectedValueOnce(new Error("bad diagram"));
    render(<MarkdownRenderer text={"```mermaid\n坏图\n```"} />);
    await waitFor(() => expect(screen.getByTestId("diagram-code")).toBeInTheDocument());
    expect(screen.getByText("坏图")).toBeInTheDocument();
  });

  it("highlightMentions：前缀名/长名优先（无歧义）", () => {
    const root = document.createElement("div");
    root.textContent = "@市场 与 @市场调研";
    highlightMentions(root, ["市场", "市场调研"]);
    const spans = root.querySelectorAll(".text-mention");
    expect(spans).toHaveLength(2);
    expect(spans[1].textContent).toBe("@市场调研");
  });

  it("破坏者：highlightMentions 重复执行不嵌套（mentions 变化重跑场景）", () => {
    const root = document.createElement("div");
    root.textContent = "正文 @市场调研";
    highlightMentions(root, ["市场调研"]);
    highlightMentions(root, ["市场调研"]);
    expect(root.querySelectorAll(".text-mention")).toHaveLength(1);
    expect(root.textContent).toBe("正文 @市场调研");
  });
});

// ── plain 模式：user 输入原样呈现──────────────────────
describe("MarkdownRenderer · plain（user 输入）", () => {
  it("不解析 markdown，保留缩进与换行（粘贴文本不被当代码块/标题重排）", () => {
    const { container } = render(<MarkdownRenderer text={"# 不是标题\n    缩进内容"} plain />);
    expect(container.querySelector("h1")).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
    expect(container.textContent).toContain("# 不是标题");
    expect(container.textContent).toContain("    缩进内容");
    expect(container.querySelector('[data-testid="plain-body"]')?.className).toContain(
      "whitespace-pre-wrap",
    );
  });

  it("@提及仍高亮（token 切分，不改写 DOM）", () => {
    const { container } = render(
      <MarkdownRenderer text={"@市场调研 请看"} mentions={["市场调研"]} plain />,
    );
    expect(container.querySelector(".text-mention")?.textContent).toBe("@市场调研");
  });

  it("plain 路径的 @提及与 markdown 路径同口径：可点（data-mention-name + cursor-pointer）", () => {
    // 此前 plain（用户消息）只有配色、没有 data 属性 ⇒ 看着可点实际点不动（衍生修复）
    const { container } = render(
      <MarkdownRenderer text={"@市场调研 请看"} mentions={["市场调研"]} plain />,
    );
    const span = container.querySelector("[data-mention-name]");
    expect(span, "用户消息的 @ 必须带 data-mention-name 才能被点击委托命中").not.toBeNull();
    expect(span?.getAttribute("data-mention-name")).toBe("市场调研");
    expect(span?.className).toContain("cursor-pointer");
    expect(span?.className).toContain("mention-hl");
  });

  it("splitMentions：长名优先命中 + 空名单单 token", () => {
    expect(splitMentions("@市场调研 和 @市场", ["市场", "市场调研"])).toEqual([
      { text: "@市场调研", mention: true },
      { text: " 和 ", mention: false },
      { text: "@市场", mention: true },
    ]);
    expect(splitMentions("无提及", [])).toEqual([{ text: "无提及", mention: false }]);
  });
});
