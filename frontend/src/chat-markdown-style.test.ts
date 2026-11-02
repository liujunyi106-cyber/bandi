/** 功能：消息正文 Markdown 渲染回归（样式表契约）——
 *  Tailwind preflight 会清掉 ol/ul/li/p 的列表符号、缩进与间距，
 *  导致 marked 渲染出的有序列表视觉上「不是分点」——此处钉住作者手写规则。 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, it } from "vitest";

// 功能：直读样式表源文件（jsdom 不解析 Tailwind 产物）
const css = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");

/** 功能：按完整选择器（形如 ".chat-md ol {" 或 ".chat-md ol,"）取其声明块。
 *  occurrence 用于跳过更早的合并规则（`.chat-md ol,` 块内也含 `.chat-md ul {` 子串）。 */
function rule(selector: string, occurrence = 1): string {
  let start = -1;
  for (let index = 0; index < occurrence; index += 1) {
    start = css.indexOf(selector, start + 1);
  }
  expect(start, `未找到规则 ${selector}（第 ${occurrence} 次）`).toBeGreaterThan(-1);
  const open = css.indexOf("{", start + selector.length - 1);
  let depth = 0;
  for (let index = open; index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, index);
    }
  }
  throw new Error(`规则 ${selector} 未闭合`);
}

it("有序/无序列表恢复列表符号与缩进（分点可见）", () => {
  const merged = rule(".chat-md ol,");
  expect(merged).toContain("list-style: revert");
  expect(merged).toContain("padding-left:");
  expect(rule(".chat-md ol {")).toContain("list-style-type: decimal");
  // `.chat-md ul {` 的第 1 次出现在合并块内（`.chat-md ol,\n.chat-md ul {`），故取第 2 次
  expect(rule(".chat-md ul {", 2)).toContain("list-style-type: disc");
});

it("段落与列表项有行间距（分行可见）", () => {
  expect(rule(".chat-md p {")).toMatch(/margin:/);
  expect(rule(".chat-md li {")).toMatch(/margin:/);
});

it("正文排版基础属性仍在", () => {
  const block = rule(".chat-md {");
  expect(block).toContain("line-height");
  expect(block).toContain("overflow-wrap");
});

it("引用/分隔线/代码/表格有可读样式", () => {
  expect(rule(".chat-md blockquote {")).toContain("border-left");
  expect(rule(".chat-md hr {")).toContain("border-top");
  expect(rule(".chat-md pre {")).toContain("background");
  expect(rule(".chat-md code {")).toContain("font-family");
  expect(rule(".chat-md table {")).toContain("border-collapse");
  expect(rule(".chat-md th,")).toContain("padding");
});