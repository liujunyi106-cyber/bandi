/**
 * 功能：Markdown 渲染纯函数（mermaid 块切分 / @提及高亮）——与组件分离
 * 便于独立测试与复用（Day137b-1）。
 */
export type MarkdownSegment =
  { type: "markdown"; content: string } | { type: "mermaid"; code: string };

/** 功能：长内容判定（含代码块/标题/列表/引用/表格/多段落 → 全宽文档布局；
 * 参考源对齐：cc-haha shouldUseDocumentLayout 同构）。 */
export function shouldUseDocumentLayout(content: string): boolean {
  const normalized = content.trim();
  if (!normalized) return false;
  if (/```/.test(normalized)) return true;
  if (/^\s{0,3}(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|\|.+\|)/m.test(normalized)) return true;
  const paragraphs = normalized
    .split(/\n\s*\n/)
    .map((chunk) => chunk.trim())
    .filter(Boolean);
  return paragraphs.length >= 2 || normalized.split("\n").filter((line) => line.trim()).length >= 8;
}

// 容错：LLM 常输出「```mermaid flowchart LR」（图表类型
// 写在语言行同行）——原正则要求 mermaid 后直接换行会漏识别；语言行余下内容并入代码。
const MERMAID_FENCE_RE = /```mermaid[ \t]*([^\n]*)\r?\n([\s\S]*?)```/gi;

/** 功能：按 ```mermaid 围栏切分文本（markdown / mermaid 段），无围栏时单 markdown 段。
 * 空白段与「纯反引号残渣」段（LLM 裸 ``` 残留）跳过——否则渲染成空 pre 灰条。 */
const FENCE_ONLY_RE = /^[`\s]*$/;

export function splitMermaidBlocks(text: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  let lastIndex = 0;
  const pushMarkdown = (content: string): void => {
    if (content.trim() && !FENCE_ONLY_RE.test(content.replace(/\n/g, ""))) {
      segments.push({ type: "markdown", content });
    }
  };
  for (const match of text.matchAll(MERMAID_FENCE_RE)) {
    const index = match.index ?? 0;
    if (index > lastIndex) {
      pushMarkdown(text.slice(lastIndex, index));
    }
    const head = (match[1] ?? "").trim();
    const body = (match[2] ?? "").trim();
    segments.push({ type: "mermaid", code: head ? `${head}\n${body}` : body });
    lastIndex = index + match[0].length;
  }
  if (lastIndex < text.length) {
    pushMarkdown(text.slice(lastIndex));
  }
  if (!segments.length) {
    segments.push({ type: "markdown", content: text });
  }
  return segments;
}

/** 功能：@提及高亮 span 的标识类（重复执行时据此跳过，防嵌套包裹）。 */
export const MENTION_HIGHLIGHT_CLASS = "mention-hl";

/** 功能：@提及名单 → 匹配正则（长名优先，防短名前缀吃掉长名）；空名单返回 null。 */
function mentionPattern(mentions: string[]): RegExp | null {
  const names = mentions
    .filter(Boolean)
    .sort((a, b) => b.length - a.length)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!names.length) return null;
  return new RegExp(`@(?:${names.join("|")})`, "g");
}

/** @提及 token：mention=true 的片段需包主色 span（纯文本渲染路径）。 */
export interface MentionToken {
  text: string;
  mention: boolean;
}

/** 功能：把纯文本按 @提及名单切成 token 序列（React 节点化渲染，不触碰 DOM）。 */
export function splitMentions(text: string, mentions: string[]): MentionToken[] {
  const pattern = mentionPattern(mentions);
  if (!pattern) return [{ text, mention: false }];
  const tokens: MentionToken[] = [];
  let lastIndex = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > lastIndex) tokens.push({ text: text.slice(lastIndex, index), mention: false });
    tokens.push({ text: match[0], mention: true });
    lastIndex = index + match[0].length;
  }
  if (lastIndex < text.length) tokens.push({ text: text.slice(lastIndex), mention: false });
  return tokens;
}

/** 功能：@提及高亮——在已渲染 DOM 的文本节点上包裹主色 span（跳过 code/pre/a 内的 @）。 */
export function highlightMentions(root: HTMLElement, mentions: string[]): void {
  const pattern = mentionPattern(mentions);
  if (!pattern) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node: Node) {
      const parent = node.parentElement;
      // MENTION_HIGHLIGHT_CLASS：mentions 变化触发重跑时跳过已包裹节点（防 span 嵌套）
      if (!parent || parent.closest(`code, pre, a, .${MENTION_HIGHLIGHT_CLASS}`)) {
        return NodeFilter.FILTER_REJECT;
      }
      pattern.lastIndex = 0;
      return pattern.test((node as Text).data)
        ? NodeFilter.FILTER_ACCEPT
        : NodeFilter.FILTER_REJECT;
    },
  });
  const targets: Text[] = [];
  while (walker.nextNode()) targets.push(walker.currentNode as Text);
  for (const node of targets) {
    const text = node.data;
    const fragment = document.createDocumentFragment();
    let lastIndex = 0;
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      const index = match.index ?? 0;
      if (index > lastIndex) {
        fragment.appendChild(document.createTextNode(text.slice(lastIndex, index)));
      }
      const span = document.createElement("span");
      // v5：@提及可点击联动右栏员工视图——data-mention-name 供容器级事件委托
      span.className = `text-mention font-medium cursor-pointer ${MENTION_HIGHLIGHT_CLASS}`;
      span.dataset.mentionName = match[0].slice(1);
      span.textContent = match[0];
      fragment.appendChild(span);
      lastIndex = index + match[0].length;
    }
    if (!fragment.childNodes.length) continue;
    if (lastIndex < text.length) {
      fragment.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
    node.parentNode?.replaceChild(fragment, node);
  }
}
