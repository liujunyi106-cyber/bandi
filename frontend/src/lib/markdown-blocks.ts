/**
 * 功能：Markdown 块模型——解析/序列化往返、块类型定义（编辑器与导出共用）。
 * 覆盖：frontmatter、段落、H1–H6、引用、代码、mermaid、无序/有序/待办列表、高亮、分割线、表格、分栏、媒体。
 */

export type BlockType =
  | "frontmatter"
  | "text"
  | "h1"
  | "h2"
  | "h3"
  | "h4"
  | "h5"
  | "h6"
  | "quote"
  | "code"
  | "diagram"
  | "ul"
  | "ol"
  | "todo"
  | "highlight"
  | "divider"
  | "table"
  | "columns"
  | "image"
  | "video"
  | "file";

export interface Block {
  id: string;
  type: BlockType;
  text: string;
  align?: "left" | "center" | "right";
  language?: string;
}

let counter = 0;

export function newBlock(type: BlockType, text = ""): Block {
  counter += 1;
  return { id: `b${Date.now().toString(36)}-${counter}`, type, text };
}

const HEADING_RE = /^(#{1,6})\s+(.*)$/;
const TODO_RE = /^[-*]\s+\[( |x|X)\]\s?(.*)$/;
const UL_RE = /^[-*]\s+(.*)$/;
const OL_RE = /^\d+\.\s+(.*)$/;
const QUOTE_RE = /^>\s?(.*)$/;
const HIGHLIGHT_RE = /^==(.+)==$/;
const TABLE_RE = /^\|.*\|$/;
const DIVIDER_RE = /^(-{3,}|\*{3,}|_{3,})$/;

export function parseMarkdown(markdown: string): Block[] {
  const blocks: Block[] = [];
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  let index = 0;

  if (lines[0]?.trim() === "---") {
    const end = lines.indexOf("---", 1);
    if (end > 0) {
      blocks.push(newBlock("frontmatter", lines.slice(1, end).join("\n")));
      index = end + 1;
    }
  }

  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === "") {
      index += 1;
      continue;
    }
    if (line.startsWith("```")) {
      const language = line.slice(3).trim();
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith("```")) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      if (language === "mermaid") {
        blocks.push(newBlock("diagram", body.join("\n")));
      } else {
        const block = newBlock("code", body.join("\n"));
        block.language = language;
        blocks.push(block);
      }
      continue;
    }
    const heading = HEADING_RE.exec(line);
    if (heading) {
      const level = heading[1].length as 1 | 2 | 3 | 4 | 5 | 6;
      blocks.push(newBlock(`h${level}` as BlockType, heading[2]));
      index += 1;
      continue;
    }
    if (DIVIDER_RE.test(line.trim())) {
      blocks.push(newBlock("divider"));
      index += 1;
      continue;
    }
    if (TABLE_RE.test(line.trim())) {
      const rows: string[] = [];
      while (index < lines.length && TABLE_RE.test(lines[index].trim())) {
        rows.push(lines[index].trim());
        index += 1;
      }
      blocks.push(newBlock("table", rows.join("\n")));
      continue;
    }
    if (line.trimStart().startsWith(":::")) {
      const label = line.trim().slice(3).trim().toLowerCase();
      const body: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith(":::")) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      if (label === "center" || label === "right" || label === "left") {
        const block = newBlock("text", body.join("\n"));
        block.align = label;
        blocks.push(block);
      } else {
        blocks.push(newBlock("columns", body.join("\n")));
      }
      continue;
    }
    const todo = TODO_RE.exec(line);
    if (todo) {
      blocks.push(newBlock("todo", `${todo[1].toLowerCase() === "x" ? "x " : ""}${todo[2]}`));
      index += 1;
      continue;
    }
    const quote = QUOTE_RE.exec(line);
    if (quote) {
      const body: string[] = [quote[1]];
      index += 1;
      while (index < lines.length && QUOTE_RE.test(lines[index])) {
        body.push(QUOTE_RE.exec(lines[index])?.[1] ?? "");
        index += 1;
      }
      blocks.push(newBlock("quote", body.join("\n")));
      continue;
    }
    const highlight = HIGHLIGHT_RE.exec(line.trim());
    if (highlight) {
      blocks.push(newBlock("highlight", highlight[1]));
      index += 1;
      continue;
    }
    const ul = UL_RE.exec(line);
    if (ul) {
      blocks.push(newBlock("ul", ul[1]));
      index += 1;
      continue;
    }
    const ol = OL_RE.exec(line);
    if (ol) {
      blocks.push(newBlock("ol", ol[1]));
      index += 1;
      continue;
    }
    const image = /^!\[([^\]]*)\]\(([^)]+)\)$/.exec(line.trim());
    if (image) {
      blocks.push(newBlock("image", `${image[1]}|${image[2]}`));
      index += 1;
      continue;
    }
    const link = /^\[([^\]]*)\]\(([^)]+)\)$/.exec(line.trim());
    if (link) {
      blocks.push(newBlock("file", `${link[1]}|${link[2]}`));
      index += 1;
      continue;
    }
    blocks.push(newBlock("text", line.replace(/<br\s*\/?>/gi, "\n")));
    index += 1;
  }
  if (blocks.length === 0) blocks.push(newBlock("text"));
  return blocks;
}

function prefixFor(type: BlockType): string {
  switch (type) {
    case "h1":
      return "# ";
    case "h2":
      return "## ";
    case "h3":
      return "### ";
    case "h4":
      return "#### ";
    case "h5":
      return "##### ";
    case "h6":
      return "###### ";
    default:
      return "";
  }
}

export function serializeBlocks(blocks: Block[]): string {
  const parts: string[] = [];
  for (const block of blocks) {
    let rendered: string;
    switch (block.type) {
      case "frontmatter":
        rendered = `---\n${block.text}\n---`;
        break;
      case "h1":
      case "h2":
      case "h3":
      case "h4":
      case "h5":
      case "h6":
        rendered = `${prefixFor(block.type)}${block.text}`;
        break;
      case "quote":
        rendered = block.text
          .split("\n")
          .map((line) => `> ${line}`)
          .join("\n");
        break;
      case "code":
        rendered = "```" + (block.language ?? "") + "\n" + block.text + "\n```";
        break;
      case "diagram":
        rendered = "```mermaid\n" + block.text + "\n```";
        break;
      case "ul":
        rendered = `- ${block.text}`;
        break;
      case "ol":
        rendered = `1. ${block.text}`;
        break;
      case "todo": {
        const checked = block.text.startsWith("x ");
        rendered = `- [${checked ? "x" : " "}] ${checked ? block.text.slice(2) : block.text}`;
        break;
      }
      case "highlight":
        rendered = `==${block.text}==`;
        break;
      case "divider":
        rendered = "---";
        break;
      case "table":
        rendered = block.text;
        break;
      case "columns":
        rendered = `::: columns\n${block.text}\n:::`;
        break;
      case "image": {
        const [alt, url] = block.text.split("|");
        rendered = `![${alt ?? ""}](${url ?? ""})`;
        break;
      }
      case "file":
      case "video": {
        const [label, url] = block.text.split("|");
        rendered = `[${label ?? ""}](${url ?? ""})`;
        break;
      }
      default:
        rendered = block.type === "text" ? block.text.replace(/\n/g, "<br>") : block.text;
    }
    if (block.align && block.align !== "left" && ALIGNABLE.has(block.type)) {
      rendered = `:::${block.align}\n${rendered}\n:::`;
    }
    parts.push(rendered);
  }
  return parts.join("\n\n") + "\n";
}

const ALIGNABLE: ReadonlySet<BlockType> = new Set<BlockType>([
  "text",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
]);

export const FORMAT_ITEMS: { type: BlockType; labelKey: string }[] = [
  { type: "text", labelKey: "knowledge.block.text" },
  { type: "h1", labelKey: "knowledge.block.h1" },
  { type: "h2", labelKey: "knowledge.block.h2" },
  { type: "h3", labelKey: "knowledge.block.h3" },
  { type: "h4", labelKey: "knowledge.block.h4" },
  { type: "h5", labelKey: "knowledge.block.h5" },
  { type: "h6", labelKey: "knowledge.block.h6" },
  { type: "quote", labelKey: "knowledge.block.quote" },
  { type: "code", labelKey: "knowledge.block.code" },
  { type: "ul", labelKey: "knowledge.block.ul" },
  { type: "ol", labelKey: "knowledge.block.ol" },
  { type: "todo", labelKey: "knowledge.block.todo" },
  { type: "highlight", labelKey: "knowledge.block.highlight" },
];

export const INSERT_ITEMS: { type: BlockType; labelKey: string }[] = [
  { type: "text", labelKey: "knowledge.block.text" },
  { type: "image", labelKey: "knowledge.block.image" },
  { type: "file", labelKey: "knowledge.block.file" },
  { type: "h1", labelKey: "knowledge.block.h1" },
  { type: "h2", labelKey: "knowledge.block.h2" },
  { type: "h3", labelKey: "knowledge.block.h3" },
  { type: "h4", labelKey: "knowledge.block.h4" },
  { type: "h5", labelKey: "knowledge.block.h5" },
  { type: "h6", labelKey: "knowledge.block.h6" },
  { type: "ul", labelKey: "knowledge.block.ul" },
  { type: "ol", labelKey: "knowledge.block.ol" },
  { type: "todo", labelKey: "knowledge.block.todo" },
  { type: "highlight", labelKey: "knowledge.block.highlight" },
  { type: "quote", labelKey: "knowledge.block.quote" },
  { type: "code", labelKey: "knowledge.block.code" },
  { type: "diagram", labelKey: "knowledge.block.diagram" },
  { type: "table", labelKey: "knowledge.block.table" },
  { type: "divider", labelKey: "knowledge.block.divider" },
  { type: "columns", labelKey: "knowledge.block.columns" },
];
