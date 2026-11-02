/**
 * 功能：从过程节点提取「引用文件」（read/grep/glob 的路径参数）——
 * 去重、限量、保序；无引用返回空数组（回复卡片引用行数据源）。
 */
import type { ProcessNode } from "@/stores/chat-store";

const REF_TOOL_NAMES = new Set(["read", "grep", "glob"]);
const MAX_REFERENCED_FILES = 10;
const PATH_KEYS = ["file_path", "filePath", "path", "pattern"] as const;

export function extractReferencedFiles(process?: ProcessNode[]): string[] {
  if (!process?.length) return [];
  const seen = new Set<string>();
  const files: string[] = [];
  for (const node of process) {
    if (node.kind !== "tool" || !REF_TOOL_NAMES.has(node.toolName)) continue;
    const input = (node.input ?? {}) as Record<string, unknown>;
    let value = "";
    for (const key of PATH_KEYS) {
      const raw = input[key];
      if (typeof raw === "string" && raw.trim()) {
        value = raw.trim();
        break;
      }
    }
    if (!value || seen.has(value)) continue;
    seen.add(value);
    files.push(value);
    if (files.length >= MAX_REFERENCED_FILES) break;
  }
  return files;
}
