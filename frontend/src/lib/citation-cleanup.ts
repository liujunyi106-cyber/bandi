/**
 * 功能：删除模型在回复末尾自写的来源清单（引用来源/参考来源等）。
 * 只处理尾部块且块内必须有引用特征，避免误删正文中的「来源」表述；
 * 整条回复只有来源清单时保留原文。与后端 strip_generated_citation_block 同规则。
 */

const HEADER =
  /^\s{0,3}(?:#{1,6}\s+)?(?:\*\*|__)?\s*(?:引用来源|参考来源|资料来源|信息来源|来源)\s*(?:\*\*|__)?\s*(?:[:：].*)?$/;
const EXPLICIT_HEADER =
  /^\s{0,3}(?:#{1,6}\s+)?(?:\*\*|__)?\s*(?:引用来源|参考来源|资料来源|信息来源)\s*(?:\*\*|__)?\s*(?:[:：].*)?$/;
const LIST_LINE = /^\s*(?:[-*•]|\d+[.、])\s+/;
const STRONG_MARKERS = ["chunk_id", "document_id", "bucket_id", "知识库条目", "source_path"];
const LABEL = /\[\d+\]/;
const URL_PATTERN = /https?:\/\//i;
const FILE_PATTERN = /\.(?:md|markdown|pdf|docx?|xlsx?|csv|txt|json)\b/i;
const MAX_LINES = 40;
const MAX_CHARS = 6000;

function looksLikeCitationList(block: string): boolean {
  const lowered = block.toLowerCase();
  if (STRONG_MARKERS.some((marker) => lowered.includes(marker))) return true;
  if (LABEL.test(block) || URL_PATTERN.test(block)) return true;
  return FILE_PATTERN.test(block);
}

export function stripGeneratedCitationBlock(content: string): string {
  if (!content.trim()) return content;
  const lines = content.split(/\r?\n/);
  let headerIndex = -1;
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    if (HEADER.test(lines[index])) {
      headerIndex = index;
      break;
    }
  }
  if (headerIndex < 0) return content;
  if (lines.length - headerIndex > MAX_LINES) return content;
  const block = lines.slice(headerIndex).join("\n");
  if (block.length > MAX_CHARS) return content;
  if (!looksLikeCitationList(block)) return content;
  if (!EXPLICIT_HEADER.test(lines[headerIndex])) {
    const listLines = lines.slice(headerIndex + 1).filter((line) => LIST_LINE.test(line));
    const labels = block.match(/\[\d+\]/g)?.length ?? 0;
    if (listLines.length < 2 && labels < 2) return content;
  }
  const before = lines.slice(0, headerIndex).join("\n");
  if ((before.match(/```/g)?.length ?? 0) % 2 === 1) return content;
  const kept = before.trimEnd();
  return kept.trim() ? kept : content;
}
