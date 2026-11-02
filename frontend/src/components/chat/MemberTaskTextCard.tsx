/**
 * 功能：正文「## 成员任务」段 → 卡片区（串行派单的「下一个执行成员」任务卡）。
 *
 * 用户拍板（2026-10-16）：成员派发不作文本平铺——从回复正文抽出「成员任务」段，
 * 渲染成内置卡片（默认折叠，可展开完整任务）；位置在正文其余内容（含风险）之后。
 * 并行派发不写该段（执行链表已足够）；串行链只写下一个执行成员。
 */
import { ChevronDown, ListTodo } from "lucide-react";
import { useState } from "react";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

/** 功能：从回复正文抽出「成员任务」段（至下一个标题或文末）。
 *  兼容两种形态：`## 成员任务`（各级标题）与 `**成员任务**` 加粗行。 */
export function extractMemberTaskSection(text: string): { body: string; section: string } {
  const match =
    /(?:^|\n)(?:#{1,4}[ \t]*成员任务\*{0,2}|\*\*成员任务\*\*)[^\n]*\n([\s\S]*?)(?=\n#{1,4}[ \t]|\s*$)/.exec(
      text,
    );
  if (!match || !match[1].trim()) {
    return { body: text, section: "" };
  }
  const section = match[1].trim();
  const body = text
    .replace(match[0], "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return { body, section };
}

/** 功能：按「成员任务 N」标记切块（无编号时整段一块）。 */
function splitTaskBlocks(section: string): string[] {
  const lines = section.split("\n");
  const blocks: string[] = [];
  let current: string[] = [];
  const marker = /(?:\*\*)?成员任务\s*\d*/;
  for (const line of lines) {
    if (marker.test(line) && current.length > 0) {
      blocks.push(current.join("\n").trim());
      current = [line];
    } else {
      current.push(line);
    }
  }
  if (current.join("\n").trim()) blocks.push(current.join("\n").trim());
  return blocks.filter(Boolean);
}

/** 功能：剥离内联 HTML 标签（保留文本；模型偶发用 `<b>` 等标记）。 */
function stripInlineHtml(value: string): string {
  return value.replace(/<\/?(?:b|strong|i|em|u|code|span|br\s*\/?)[^>]*>/gi, "");
}

/** 功能：成员任务块解析——模型可能用 `<details><summary>标题</summary>正文</details>`
 *  结构（折叠态渲染标题与正文，不显示 `<details>` 标签）。
 *  提取 summary 为标题、details 内部为正文；无 details 时首行作标题。 */
function normalizeBlockText(text: string): { title: string; body: string } {
  const summaryMatch = /<summary>([\s\S]*?)<\/summary>/i.exec(text);
  // 剥离模型附带的「（点击展开）」类提示词——展开操作由卡片自身的箭头承担
  const summaryTitle = summaryMatch
    ? stripInlineHtml(summaryMatch[1])
        .replace(/（点击展开）|\(点击展开\)|（单击展开）|\(Click to expand\)/gi, "")
        .trim()
    : "";
  if (summaryTitle) {
    const body = stripInlineHtml(
      text
        .replace(/<details[^>]*>/gi, "")
        .replace(/<\/details>/gi, "")
        .replace(/<summary>[\s\S]*?<\/summary>/gi, ""),
    ).trim();
    return { title: summaryTitle, body };
  }
  const [firstLine = "", ...restLines] = text.split("\n");
  return {
    title: stripInlineHtml(firstLine).replace(/^\*\*|\*\*$/g, "").trim(),
    body: stripInlineHtml(restLines.join("\n")).trim(),
  };
}

function TaskBlock({ text, defaultOpen }: { text: string; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const { title, body: rest } = normalizeBlockText(text);
  return (
    <div
      data-testid="member-task-text-block"
      className="border-chat-hairline rounded-lg border px-2.5 py-1.5"
    >
      <div className="flex items-start gap-1.5">
        <button
          type="button"
          data-testid="member-task-text-toggle"
          className="text-chat-muted hover:text-chat-ink mt-0.5 shrink-0"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </button>
        <p className="text-chat-ink min-w-0 flex-1 text-xs font-medium break-words">
          {title || "成员任务"}
        </p>
      </div>
      {open && rest ? (
        <div className="text-chat-muted mt-1 pl-5 text-[11px] whitespace-pre-wrap">{rest}</div>
      ) : null}
    </div>
  );
}

export function MemberTaskTextCards({ section }: { section: string }) {
  const t = useT();
  if (!section.trim()) return null;
  const blocks = splitTaskBlocks(section);
  return (
    <div
      data-testid="member-task-text-cards"
      data-count={blocks.length}
      className="flex w-full flex-col gap-1.5"
    >
      <p className="text-chat-muted flex items-center gap-1 text-xs font-medium">
        <ListTodo className="size-3.5" />
        {t("chat.memberTask.title")}
      </p>
      {blocks.map((block, index) => (
        <TaskBlock key={index} text={block} defaultOpen={blocks.length === 1} />
      ))}
    </div>
  );
}
