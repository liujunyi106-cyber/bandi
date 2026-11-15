/**
 * 功能：工具专属卡片契约——按工具语义归形态，并断言渲染出的结构。
 *
 * 钉行为：终端工具出现命令与输出、编辑工具出现逐行着色、读取工具出现路径与行数。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ToolCard, toolCardIcon, toolCardKind, toolCardKindOf } from "./ToolCard";
describe("toolCardKind · 工具按语义归类", () => {
  it("命令类工具归为终端", () => {
    expect(toolCardKind("bash")).toBe("terminal");
    expect(toolCardKind("python_exec")).toBe("terminal");
    expect(toolCardKind("SHELL")).toBe("terminal");
  });

  it("编辑类工具归为差异", () => {
    expect(toolCardKind("edit")).toBe("diff");
    expect(toolCardKind("multi_edit")).toBe("diff");
    expect(toolCardKind("apply_patch")).toBe("diff");
  });

  it("读取与检索类分别归为 read / search", () => {
    expect(toolCardKind("read")).toBe("read");
    expect(toolCardKind("grep")).toBe("search");
    expect(toolCardKind("list_dir")).toBe("search");
  });

  it("未识别的工具回落通用形态", () => {
    expect(toolCardKind("mcp_unknown_thing")).toBe("generic");
    expect(toolCardKind("")).toBe("generic");
  });
});

describe("toolCardKindOf · 行级判定", () => {
  it("有工具名即按其归类", () => {
    expect(toolCardKindOf("bash")).toBe("terminal");
    expect(toolCardKindOf("edit")).toBe("diff");
  });

  it("无工具名（思考/技能/注记行）回落通用", () => {
    expect(toolCardKindOf(undefined)).toBe("generic");
    expect(toolCardKindOf("")).toBe("generic");
  });
});

describe("toolCardKindOf · 后端声明优先于按名回落", () => {
  it("声明为 terminal 时即使工具名陌生也走终端卡片", () => {
    expect(toolCardKindOf("mcp_custom_runner", "terminal")).toBe("terminal");
    expect(toolCardKindOf("weird_name", "diff")).toBe("diff");
  });

  it("无声明时按工具名回落（旧数据不受影响）", () => {
    expect(toolCardKindOf("bash")).toBe("terminal");
    expect(toolCardKindOf("bash", "")).toBe("terminal");
  });

  it("未知声明值被忽略，不产生空卡片", () => {
    expect(toolCardKindOf("bash", "not-a-kind")).toBe("terminal");
    expect(toolCardKindOf(undefined, "not-a-kind")).toBe("generic");
  });
});

describe("ToolCard · 专属结构", () => {
  it("终端卡片渲染命令提示符与输出", () => {
    render(<ToolCard kind="terminal" input={{ command: "pytest -q" }} output="3 passed" />);
    expect(screen.getByText("$")).toBeTruthy();
    expect(screen.getByText("pytest -q")).toBeTruthy();
    expect(screen.getByText("3 passed")).toBeTruthy();
  });

  it("差异卡片对增删行着色", () => {
    const { container } = render(<ToolCard kind="diff" input={{}} output={"-old\n+new\n same"} />);
    const rows = Array.from(container.querySelectorAll("div"));
    expect(rows.some((r) => r.className.includes("text-rose-600"))).toBe(true);
    expect(rows.some((r) => r.className.includes("text-emerald-600"))).toBe(true);
  });

  it("读取卡片给出路径与行数摘要", () => {
    render(<ToolCard kind="read" input={{ path: "bandi/pyproject.toml" }} output={"a\nb\nc"} />);
    expect(screen.getByText("bandi/pyproject.toml")).toBeTruthy();
    expect(screen.getByText("3 行")).toBeTruthy();
  });

  it("通用形态不渲染任何结构", () => {
    const { container } = render(<ToolCard kind="generic" input={{ x: "y" }} output="z" />);
    expect(container.textContent).toBe("");
  });

  it("形态图标对每种形态都有定义", () => {
    for (const kind of ["terminal", "diff", "read", "search", "write", "generic"] as const) {
      expect(toolCardIcon(kind)).toBeTruthy();
    }
  });
});
