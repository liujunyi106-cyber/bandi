/**
 * 功能：消息正文渲染器（marked 解析 + DOMPurify 净化 + Mermaid 懒加载）——
 * 三视图（对话/群聊/单聊）共用。两种模式：
 * - 默认 markdown：assistant 输出（@提及高亮为渲染后 DOM 文本节点处理，跳过代码块）；
 * - plain：user 输入原样呈现（`whitespace-pre-wrap` 保留缩进/换行，@提及走 token 切分）
 * 避免用户粘贴的缩进文本被当作代码块/列表重排。
 * mermaid 图表：动态 import 分块加载；code 稳定后渲染，重渲染期间保留上一版 SVG（防流式闪烁）；
 * 加载中/失败回落原始代码块（渐进增强）。
 */
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useMemo, useRef } from "react";

import { DiagramCard } from "@/components/chat/DiagramCard";

import {
  MENTION_HIGHLIGHT_CLASS,
  highlightMentions,
  splitMentions,
  splitMermaidBlocks,
} from "@/lib/markdown";
import { cn } from "@/lib/utils";

// 外链统一新开窗口（渲染后处理，一次注册全局生效）
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node instanceof Element && node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const TABLE_SEPARATOR = /^\|[\s:|\-]+\|$/;

/** 功能：为缺少表头的表格补一行空表头（模型常直接输出 |---| 开头，marked 不认）。 */
function normalizeTables(text: string): string {
  if (!text.includes("|")) return text;
  const out: string[] = [];
  for (const line of text.split("\n")) {
    if (TABLE_SEPARATOR.test(line.trim())) {
      const previous = (out[out.length - 1] ?? "").trim();
      if (!(previous.startsWith("|") && previous.endsWith("|"))) {
        const columns = Math.max(1, (line.match(/\|/g) ?? []).length - 1);
        out.push("|" + "  |".repeat(columns));
      }
    }
    out.push(line);
  }
  return out.join("\n");
}

/** 功能：剥离空的 ``` 围栏（模型常在正文前后多吐一对空围栏，marked 会渲染成空 pre 灰条）。 */
function stripEmptyCodeFences(text: string): string {
  return text.replace(/```[^\n]*\r?\n[ \t]*\r?\n?```[ \t]*(?:\r?\n|$)/g, "");
}

function stripEmptyRenderedBlocks(html: string): string {
  return html
    .replace(/<pre><code[^>]*>\s*<\/code><\/pre>/g, "")
    .replace(/<p>\s*<\/p>/g, "")
    .replace(/<hr\s*\/?>/g, "");
}

function renderMarkdown(text: string): string {
  // breaks:false 保留 单个换行 转 <br> 会让 GFM 表格行被拆散导致表格渲染失败
  // 段落内换行显示为原始换行的文本。
  const html = marked.parse(stripEmptyCodeFences(normalizeTables(text)), {
    async: false,
    gfm: true,
    breaks: false,
  }) as string;
  // FORBID_ATTR：禁用 style，防止表格列宽、position/z-index 伪布局节点等
  const clean = DOMPurify.sanitize(html, {
    FORBID_ATTR: ["style"],
    FORBID_TAGS: ["style", "svg", "math", "foreignObject", "form"],
  });
  return stripEmptyRenderedBlocks(clean);
}

// 图表渲染（mermaid/SVG）抽至 DiagramCard（卡片自带「…」菜单与代码↔图切换）

interface MarkdownRendererProps {
  text: string;
  /** @提及高亮名单（群聊场景；渲染后在 DOM 文本节点上处理）。 */
  mentions?: string[];
  /** true = 原样呈现（user 输入；保留缩进换行，不解析 markdown）。 */
  plain?: boolean;
  className?: string;
}

function MarkdownBody({ text, mentions, className }: MarkdownRendererProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const parts = useMemo(
    () =>
      splitMermaidBlocks(text).map((segment) =>
        segment.type === "markdown"
          ? { type: "markdown" as const, html: renderMarkdown(segment.content) }
          : segment,
      ),
    [text],
  );

  useEffect(() => {
    const root = rootRef.current;
    if (!root || !mentions?.length) return;
    highlightMentions(root, mentions);
  }, [parts, mentions]);

  return (
    <div ref={rootRef} className={cn("chat-md", className)} data-testid="markdown-body">
      {parts.map((part, index) =>
        part.type === "mermaid" ? (
          <DiagramCard key={index} code={part.code} />
        ) : (
          <div key={index} dangerouslySetInnerHTML={{ __html: part.html }} />
        ),
      )}
    </div>
  );
}

function PlainBody({ text, mentions, className }: MarkdownRendererProps) {
  const tokens = useMemo(() => splitMentions(text, mentions ?? []), [text, mentions]);
  return (
    <div className={cn("whitespace-pre-wrap break-words", className)} data-testid="plain-body">
      {tokens.map((token, index) =>
        token.mention ? (
          // 与 markdown 路径（highlightMentions）**同口径**：主色 + 可点联动的 data 属性——
          // 此前 plain 路径只有配色没有 data-mention-name，用户消息里的 @ 看着可点实际点不动。
          <span
            key={index}
            className={`text-mention font-medium cursor-pointer ${MENTION_HIGHLIGHT_CLASS}`}
            data-mention-name={token.text.slice(1)}
          >
            {token.text}
          </span>
        ) : (
          <span key={index}>{token.text}</span>
        ),
      )}
    </div>
  );
}

export function MarkdownRenderer(props: MarkdownRendererProps) {
  return props.plain ? <PlainBody {...props} /> : <MarkdownBody {...props} />;
}
