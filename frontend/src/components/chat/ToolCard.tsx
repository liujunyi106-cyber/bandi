/**
 * 功能：工具专属卡片——按工具语义把一次调用渲染成 Terminal / Diff / Read / Search / Write 形态。
 *
 * 判定完全由 toolName 与 input 推导，后端不需要额外声明渲染意图；
 * 未识别的工具回落到通用行（Generic）。
 */

import { Diff, FileText, FolderTree, Search, SquareTerminal } from "lucide-react";

import { cn } from "@/lib/utils";

/** 功能：卡片形态。 */
export type ToolCardKind = "terminal" | "diff" | "read" | "search" | "write" | "generic";

/** 功能：按工具名归类卡片形态（同族工具共享一种形态）。 */
export function toolCardKind(toolName: string): ToolCardKind {
  const name = toolName.toLowerCase();
  if (["bash", "shell", "python_exec", "pwsh", "run_command"].includes(name)) return "terminal";
  if (["edit", "multi_edit", "apply_patch", "str_replace"].includes(name)) return "diff";
  if (["read", "file_read", "doc_read", "word_reader", "excel_reader", "pdf_reader"].includes(name))
    return "read";
  if (["grep", "glob", "search", "web_search", "knowledge_search"].includes(name)) return "search";
  if (["write", "file_write", "create_file"].includes(name)) return "write";
  if (name === "list_dir") return "search";
  return "generic";
}

const KIND_ICON = {
  terminal: SquareTerminal,
  diff: Diff,
  read: FileText,
  search: Search,
  write: FileText,
  generic: FolderTree,
} as const;

/** 功能：从工具入参里取最像「目标」的一个字符串（路径/命令/查询）。 */
function primaryTarget(input: Record<string, unknown> | undefined): string {
  if (!input) return "";
  for (const key of ["command", "cmd", "path", "file_path", "query", "pattern", "dir", "target"]) {
    const value = input[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  const first = Object.values(input).find((v) => typeof v === "string" && v.trim());
  return typeof first === "string" ? first.trim() : "";
}

/** 功能：终端卡片——命令单行 + 输出块（等宽、限高滚动）。 */
function TerminalBody({ command, output }: { command: string; output: string }) {
  return (
    <div className="bg-chat-rail/40 mt-1 overflow-hidden rounded-md">
      <div className="text-chat-ink flex items-start gap-1.5 px-2 py-1 font-mono text-[11px] leading-relaxed">
        <span className="text-chat-muted/70 shrink-0 select-none">$</span>
        <span className="min-w-0 flex-1 whitespace-pre-wrap break-all">{command}</span>
      </div>
      {output ? (
        <pre className="text-chat-muted max-h-40 overflow-auto border-t border-black/5 px-2 py-1 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
          {output}
        </pre>
      ) : null}
    </div>
  );
}

/** 功能：差异卡片——逐行着色（+ 增 / - 删 / 其余上下文）。 */
function DiffBody({ text }: { text: string }) {
  const lines = text.split("\n").slice(0, 40);
  return (
    <pre className="bg-chat-rail/40 text-chat-muted mt-1 max-h-52 overflow-auto rounded-md px-2 py-1 font-mono text-[11px] leading-relaxed">
      {lines.map((line, index) => (
        <div
          key={`${index}-${line.slice(0, 12)}`}
          className={cn(
            "whitespace-pre-wrap break-words",
            line.startsWith("+") && "text-emerald-600",
            line.startsWith("-") && "text-rose-600",
          )}
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

/** 功能：读取卡片——文件路径 + 行数/字节摘要。 */
function ReadBody({ path, output }: { path: string; output: string }) {
  const lineCount = output ? output.split("\n").length : 0;
  return (
    <div className="mt-0.5 flex min-w-0 items-center gap-2 text-[11px]">
      <span className="text-chat-muted truncate font-mono">{path}</span>
      {lineCount > 0 ? (
        <span className="text-chat-muted/60 shrink-0 tabular-nums">{lineCount} 行</span>
      ) : null}
    </div>
  );
}

/** 功能：工具专属卡片主体；generic 不渲染额外结构（由调用方保留原行）。 */
export function ToolCard({
  kind,
  input,
  output,
}: {
  kind: ToolCardKind;
  input: Record<string, unknown> | undefined;
  output: string;
}) {
  if (kind === "generic") return null;
  const target = primaryTarget(input);

  if (kind === "terminal") return <TerminalBody command={target} output={output} />;
  if (kind === "diff") return <DiffBody text={output || target} />;
  if (kind === "read") return <ReadBody path={target} output={output} />;
  if (kind === "write") return <ReadBody path={target} output={output} />;
  // search：查询词 + 命中摘要
  return (
    <div className="bg-chat-rail/40 mt-1 overflow-hidden rounded-md">
      <div className="text-chat-muted px-2 py-1 font-mono text-[11px] break-all">{target}</div>
      {output ? (
        <pre className="text-chat-muted/90 max-h-32 overflow-auto border-t border-black/5 px-2 py-1 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
          {output}
        </pre>
      ) : null}
    </div>
  );
}

/** 功能：卡片形态图标（供行前缀复用，保持与形态一致）。 */
export function toolCardIcon(kind: ToolCardKind) {
  return KIND_ICON[kind];
}

/** 功能：工具行是否该用专属卡片——有 toolName 且能归类到具体形态。 */
export function toolCardKindOf(toolName: string | undefined): ToolCardKind {
  if (!toolName) return "generic";
  return toolCardKind(toolName);
}
