/**
 * 过程展示的纯逻辑：思考摘要提取 + 工具聚合计数。
 *
 * - 摘要提取用**规则正则**，不调 LLM —— opencode `packages/tui/src/context/thinking.ts:8`
 * 与 cc-haha / pi / LeAgent 的工具组摘要一致；cc-haha 的 Haiku 摘要机制注释明说
 * `, ignore them in stream processing`（`utils/messages.ts:3076-3078`）
 * 桌面端根本不消费 ⇒ 加 LLM 往返是纯浪费。
 * - 聚合计数借 opencode `contextToolSummary`（`session-ui/.../message-part.tsx:916`）：
 * 纯计数，不生成自然语言。
 */
import type { ProcessNode } from "@/stores/chat-store";

/** 摘要标题最大长度（cc-haha `toolUseSummaryGenerator.ts:15-24` 约束 30 字符，向上放宽到 40）。 */
const MAX_TITLE = 40;

/**
 * 从思考文本提取「摘要标题 + 正文」。
 *
 * 优先级（借 opencode `reasoningSummary`）：markdown 标题 → 加粗首行 → 首句截断。
 * 纯函数、无副作用、失败回落为空标题（不抛）。
 */
export function thinkingSummary(text: string): { title: string; body: string } {
  const content = (text ?? "").replace(/\r\n?/g, "\n").trim();
  if (!content) return { title: "", body: "" };

  // 1) markdown 标题（# ~ ######）
  const heading = content.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/m);
  if (heading) {
    const title = clamp(heading[1]);
    if (title) return { title, body: content.replace(heading[0], "").trim() };
  }

  // 2) 加粗首行（opencode 口径：**Title**\n\nBody）
  const bold = content.match(/^\s*\*\*([^*\n]+)\*\*\s*(?:\n|$)/);
  if (bold) {
    const title = clamp(bold[1]);
    if (title) return { title, body: content.replace(bold[0], "").trim() };
  }

  // 3) 首句（按中英文句末标点切），超长再截断
  const sentence = content.match(/^[\s\S]*?[。！？.!?](?=\s|$|[\u4e00-\u9fff])/);
  const head = sentence ? sentence[0] : (content.split("\n")[0] ?? "");
  return { title: clamp(head), body: content };
}

function clamp(raw: string): string {
  const oneLine = raw.replace(/\s+/g, " ").trim();
  if (oneLine.length <= MAX_TITLE) return oneLine;
  return `${oneLine.slice(0, MAX_TITLE - 1)}…`;
}

/** 同类工具聚合分组。 */
export interface ToolGroup {
  toolName: string;
  count: number;
  hasError: boolean;
  running: boolean;
}

/**
 * 把连续同类工具调用聚合为分组（opencode `ContextToolGroup` 口径）。
 *
 * 保持首次出现顺序（不打乱时序）；组内任一失败 ⇒ hasError；任一运行中 ⇒ running。
 */
export function aggregateToolNodes(nodes: ProcessNode[]): ToolGroup[] {
  const out: ToolGroup[] = [];
  const index = new Map<string, ToolGroup>();
  for (const node of nodes) {
    if (node.kind !== "tool") continue;
    const name = node.toolName || "unknown";
    const existing = index.get(name);
    if (existing) {
      existing.count += 1;
      if (node.status === "error") existing.hasError = true;
      if (node.status === "running") existing.running = true;
      continue;
    }
    const group: ToolGroup = {
      toolName: name,
      count: 1,
      hasError: node.status === "error",
      running: node.status === "running",
    };
    index.set(name, group);
    out.push(group);
  }
  return out;
}

/** 整轮过程耗时（毫秒）：工具 + 思考两类节点累加；返回 null 表示无可用度量。 */export function totalDurationMs(nodes: ProcessNode[]): number | null {
  let total = 0;
  let seen = false;
  for (const node of nodes) {
    if (node.kind === "tool" || node.kind === "thought") {
      if (typeof node.durationMs === "number" && Number.isFinite(node.durationMs)) {
        total += node.durationMs;
        seen = true;
      }
    }
  }
  return seen ? total : null;
}

/** 过程块分类桶键（含「读取文件」探索类——read/glob/grep/ls 不再混入其他工具）。 */
export type BucketKey =
  | "thought"
  | "web"
  | "skill"
  | "knowledge"
  | "memory"
  | "explore"
  | "file"
  | "tool";

/** 工具输入摘要截断长度。 */
export const INPUT_PREVIEW_CHARS = 72;

/** 功能：工具名 → 中文展示名（未登记名原样返回）。 */
export const TOOL_LABELS: Record<string, string> = {
  memory_search: "记忆检索",
  memory_read: "记忆读取",
  memory_assert: "记忆写入",
  knowledge_search: "知识库检索",
  knowledge_ingest: "知识库入库",
  web_search: "联网搜索",
  web_fetch: "网页读取",
  web_scraper: "网页抓取",
  web_image_search: "图片搜索",
  request_user_input: "向用户提问",
  skill_invoke: "技能调用",
  skill_discover: "技能检索",
  ontology_query: "本体查询",
  ontology_assert: "本体断言",
  ontology_define: "本体定义",
  read: "读取文件",
  glob: "匹配文件",
  grep: "搜索内容",
  ls: "列目录",
};

/** 功能：工具名 → 分类桶。 */
export function bucketFor(toolName: string): BucketKey {
  const name = (toolName ?? "").toLowerCase();
  if (name.includes("web") || name.includes("browser")) return "web";
  if (name.includes("memory")) return "memory";
  if (name.includes("knowledge") || name.includes("ontology") || name.includes("entity")) {
    return "knowledge";
  }
  if (name === "read" || name === "glob" || name === "grep" || name === "ls") return "explore";
  if (name === "write" || name === "edit" || name === "apply_patch") return "file";
  return "tool";
}

/** 功能：工具输入 → 单行摘要（key=value 截断；无输入返回空串）。 */
export function inputPreview(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  const entries = Object.entries(input);
  if (entries.length === 0) return "";
  const text = entries.map(([key, value]) => `${key}=${JSON.stringify(value)}`).join(" ");
  return text.length > INPUT_PREVIEW_CHARS ? `${text.slice(0, INPUT_PREVIEW_CHARS)}…` : text;
}

/** 功能：从工具输入提取文件/路径引用（read 的 file_path、glob 的 pattern、ls 的 path 等）。 */
export function fileRefOf(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  for (const key of ["file_path", "path", "pattern", "file", "dir"]) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

/** 功能：工具调用 → 语义化单行摘要（读取类给路径，其余回退 key=value 预览）。 */
export function toolSummary(toolName: string, input?: Record<string, unknown>): string {
  const ref = fileRefOf(input);
  if (ref) {
    const clipped = ref.length > INPUT_PREVIEW_CHARS ? `${ref.slice(0, INPUT_PREVIEW_CHARS)}…` : ref;
    return `${toolLabel(toolName)} · ${clipped}`;
  }
  return inputPreview(input) || toolLabel(toolName);
}

/** 功能：工具名 → 中文展示名。 */
export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name;
}

/**
 * 毫秒 → 展示串（<1s 用毫秒；<60s 用一位小数秒；≥60s 用 `3m11s`）。
 *
  * ≥60s 用词式 `3m11s`——`3:11` 会被读成时钟时间而不是时长。
 * 无可用度量（null / 非法）返回占位符 `—`。
 */
export function formatDurationMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m${Math.round(seconds % 60)}s`;
}
