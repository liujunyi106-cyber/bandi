/**
 * 功能：正文「## 成员任务」段 → 卡片（提取纯函数 + 渲染折叠 + 破坏者）。
 * （2026-10-16）：成员派发不作文本平铺；串行链只写下一个执行成员。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import {
  extractMemberTaskSection,
  MemberTaskTextCards,
} from "@/components/chat/MemberTaskTextCard";
import { useUiStore } from "@/stores/ui-store";

const CONTENT = `老板，先给结论。

## 分工与执行链

| 顺序 | 交付物 | 负责人 | 状态 |
| --- | --- | --- | --- |
| 1 | 调研 | 深度研究专家 | 已派发 |

## 风险

- 供应侧证据缺口（缓释：缺口清单交付）

## 成员任务

**成员任务 1 — 派发给「深度研究专家」**
- 任务编号：R1｜工作卡 \`79bb8e3a\`
- 一句话标题：完成三路可溯源取证
- 可展开细节：需求侧 / 竞争侧 / 供应侧三类证据与验收口径`;

describe("extractMemberTaskSection（正文 → 段抽取）", () => {
  it("抽出「## 成员任务」段并从正文剥离（卡片由渲染层另置正文之后）", () => {
    const { body, section } = extractMemberTaskSection(CONTENT);
    expect(section).toContain("成员任务 1 — 派发给「深度研究专家」");
    expect(section).toContain("可展开细节");
    expect(body).not.toContain("## 成员任务");
    expect(body).toContain("## 风险");
    expect(body).toContain("## 分工与执行链");
  });

  it("破坏者：无该段 → section 为空且正文原样（并行派发不写该段）", () => {
    const plain = "结论：按北美口径推进。";
    const { body, section } = extractMemberTaskSection(plain);
    expect(section).toBe("");
    expect(body).toBe(plain);
  });

  it("兼容 `**成员任务**` 加粗行形态（模型偶发写加粗行致卡消失）", () => {
    const content = [
      "结论：链路已跑起来。",
      "",
      "**成员任务**（已派发，串行链路下一环）",
      "",
      "- 指派给：深度研究专家｜任务编号 R1",
      "- 可展开细节：三路证据",
      "",
      "## 风险",
      "- 参数未定风险",
    ].join("\n");
    const { body, section } = extractMemberTaskSection(content);
    expect(section).toContain("R1");
    expect(section).toContain("可展开细节");
    expect(body).not.toContain("**成员任务**");
    expect(body).toContain("## 风险");
  });

  it("兼容 `<details><summary>` HTML 形态（模型用 HTML 写卡）", () => {
    const content = [
      "结论文本。",
      "",
      "**成员任务**（已派发，串行链路下一环）",
      "",
      "<details>",
      "<summary><b>指派给：深度研究专家｜任务编号 R1｜候选方向证据调研</b>（点击展开）</summary>",
      "",
      "- **目标**：产出 3 个候选方向的公开证据包。",
      "</details>",
    ].join("\n");
    const { body, section } = extractMemberTaskSection(content);
    expect(section).toContain("R1");
    expect(body).not.toContain("**成员任务**");
    render(<MemberTaskTextCards section={section} />);
    const block = screen.getByTestId("member-task-text-block");
    // summary 内容成为卡标题；详情为正文——任何 HTML 标签都不得裸露
    expect(block.textContent).toContain("指派给：深度研究专家");
    expect(block.textContent).toContain("产出 3 个候选方向");
    expect(block.textContent).not.toContain("<details>");
    expect(block.textContent).not.toContain("<summary>");
    expect(block.textContent).not.toContain("<b>");
  });
});

describe("MemberTaskTextCards（渲染）", () => {
  beforeEach(() => {
    useUiStore.setState({ locale: "zh" });
  });

  it("单块默认展开：标题 + 任务内容可见", () => {
    const { section } = extractMemberTaskSection(CONTENT);
    render(<MemberTaskTextCards section={section} />);
    expect(screen.getByTestId("member-task-text-cards")).toBeInTheDocument();
    expect(screen.getByText("成员任务")).toBeInTheDocument();
    expect(screen.getByTestId("member-task-text-block")).toHaveTextContent("深度研究专家");
    expect(screen.getByText(/可展开细节/)).toBeInTheDocument();
  });

  it("折叠箭头：点击收起（再点恢复）", async () => {
    const user = userEvent.setup();
    const { section } = extractMemberTaskSection(CONTENT);
    render(<MemberTaskTextCards section={section} />);
    await user.click(screen.getByTestId("member-task-text-toggle"));
    expect(screen.queryByText(/可展开细节/)).not.toBeInTheDocument();
    await user.click(screen.getByTestId("member-task-text-toggle"));
    expect(screen.getByText(/可展开细节/)).toBeInTheDocument();
  });

  it("空段不渲染（不留空壳）", () => {
    render(<MemberTaskTextCards section="  " />);
    expect(screen.queryByTestId("member-task-text-cards")).not.toBeInTheDocument();
  });
});
