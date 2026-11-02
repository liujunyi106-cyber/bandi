/**
 * 功能：回复卡片下方的「来源」入口。
 * 3 条以内直接展示链接胶囊；超过 3 条折叠为「搜索到 N 个网页 + 头像叠加」，点击在右侧面板列出全部结果。
 */
import { Link as LinkIcon } from "lucide-react";
import { useMemo, useState } from "react";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useT } from "@/i18n";
import type { ProcessNode } from "@/stores/chat-store";

/** 来源条目（标题/摘要来自 web_search 输出，日期取回复时间）。 */
export interface SourceEntry {
  url: string;
  title: string;
  snippet: string;
  site: string;
  date: string;
}

const URL_PATTERN = /https?:\/\/[^\s"'\\<>）)】\]，。；、]+/g;
const MAX_SOURCES = 8;

/** 功能：域名（去 www）。 */
function siteOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** 功能：清理搜索标题（去 markdown 加粗与 URL 尾巴）。 */
function cleanTitle(value: string): string {
  return value
    .replace(/\*\*/g, "")
    .replace(/\s*URL:\s*https?:\/\/\S+.*$/i, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** 功能：回复时间 → YYYY/MM/DD。 */
function dateOf(timestamp?: string): string {
  if (!timestamp) return "";
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}/${String(date.getMonth() + 1).padStart(2, "0")}/${String(
    date.getDate(),
  ).padStart(2, "0")}`;
}

/** 功能：解析 web_search 格式化输出（`1. title\n URL: url - snippet`）。 */
function parseFormattedSources(text: string): Omit<SourceEntry, "site" | "date">[] {
  const rows: Omit<SourceEntry, "site" | "date">[] = [];
  const blocks = text.split(/\n(?=\s*\d+[.、]\s+)/);
  for (const block of blocks) {
    const head = block.match(/^\s*\d+[.、]\s+(.+?)\s*$/m);
    const urlMatch = block.match(
      /URL:\s*(https?:\/\/[^\s"'\\<>）)】\]，。；、]+)(?:\s*[-–]\s*([\s\S]*))?/,
    );
    if (!head || !urlMatch) continue;
    rows.push({
      title: cleanTitle(head[1]) || siteOf(urlMatch[1]),
      url: urlMatch[1].replace(/[.,;:]+$/, ""),
      snippet: (urlMatch[2] ?? "").replace(/\*\*/g, "").trim(),
    });
  }
  return rows;
}

/** 功能：从回复正文补全来源（表格行 / markdown 链接 / 裸 URL）。 */
function parseContentSources(content?: string): Omit<SourceEntry, "site" | "date">[] {
  if (!content) return [];
  const rows: Omit<SourceEntry, "site" | "date">[] = [];
  for (const line of content.split(/\r?\n/)) {
    const cells = line.includes("|")
      ? line
          .split("|")
          .map((cell) => cell.trim())
          .filter(Boolean)
      : [];
    const urlCell = cells.find((cell) => /^https?:\/\//i.test(cell));
    if (urlCell) {
      const url = urlCell.replace(/[.,;:]+$/, "");
      const title = cells.find((cell) => cell !== urlCell && !/^\d+$/.test(cell)) ?? siteOf(url);
      const snippet = cells[cells.length - 1] !== urlCell ? cells[cells.length - 1] : "";
      rows.push({
        url,
        title: cleanTitle(title) || siteOf(url),
        snippet: snippet.replace(/\*\*/g, "").trim(),
      });
      continue;
    }
    const links = [...line.matchAll(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g)];
    for (const link of links) {
      rows.push({ title: link[1].trim(), url: link[2], snippet: "" });
    }
    if (links.length > 0) continue;
    for (const match of line.matchAll(URL_PATTERN)) {
      const url = match[0].replace(/[.,;:]+$/, "");
      const prefix = line.slice(0, match.index ?? 0).replace(/[|#*\-\s]+$/g, "").trim();
      rows.push({ title: prefix || siteOf(url), url, snippet: "" });
    }
  }
  return rows;
}

/** 功能：从过程节点与回复正文提取来源（结构化优先，正文补全）。 */
export function collectSources(
  nodes?: ProcessNode[],
  timestamp?: string,
  content?: string,
): SourceEntry[] {
  const found = new Map<string, SourceEntry>();
  const date = dateOf(timestamp);
  if ((!nodes || nodes.length === 0) && !content) return [];
  const visit = (list: ProcessNode[]): void => {
    for (const node of list) {
      if (node.kind === "tool") {
        const haystack = [
          node.detail ?? "",
          node.progress ? JSON.stringify(node.progress.data) : "",
          node.input ? JSON.stringify(node.input) : "",
        ].join("\n");
        const structured = parseFormattedSources(haystack);
        for (const row of structured) {
          if (!row.url || found.has(row.url)) continue;
          found.set(row.url, { ...row, site: siteOf(row.url), date });
        }
        for (const match of haystack.matchAll(URL_PATTERN)) {
          const url = match[0].replace(/[.,;:]+$/, "");
          if (found.has(url) || found.size >= MAX_SOURCES) continue;
          found.set(url, { url, title: url, snippet: "", site: siteOf(url), date });
        }
        if (node.children?.length) visit(node.children);
      }
    }
  };
  if (nodes) visit(nodes);
  for (const row of parseContentSources(content)) {
    if (!row.url) continue;
    const existing = found.get(row.url);
    if (existing) {
      if (!existing.title || existing.title === existing.url) existing.title = row.title;
      if (!existing.snippet && row.snippet) existing.snippet = row.snippet;
      continue;
    }
    if (found.size >= MAX_SOURCES) continue;
    found.set(row.url, { ...row, site: siteOf(row.url), date });
  }
  return Array.from(found.values()).slice(0, MAX_SOURCES);
}

/** 功能：站点头像（favicon 加载失败回退首字母）。 */
function SourceAvatar({ site, className }: { site: string; className?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <span
      className={`bg-primary/5 text-chat-muted relative grid shrink-0 place-items-center overflow-hidden rounded-full text-[10px] font-semibold ${className ?? "size-6"}`}
      aria-hidden="true"
    >
      {site.slice(0, 1).toUpperCase()}
      {failed ? null : (
        <img
          src={`https://www.google.com/s2/favicons?domain=${encodeURIComponent(site)}&sz=64`}
          alt=""
          loading="lazy"
          referrerPolicy="no-referrer"
          className="absolute inset-0 size-full object-cover"
          onError={() => setFailed(true)}
        />
      )}
    </span>
  );
}

/** 功能：来源摘要行（>3 条）与链接胶囊（<=3 条）。 */
export function ReplySources({
  nodes,
  timestamp,
  content,
}: {
  nodes?: ProcessNode[];
  timestamp?: string;
  content?: string;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const sources = useMemo(
    () => collectSources(nodes, timestamp, content),
    [content, nodes, timestamp],
  );
  if (sources.length === 0) return null;

  const label = t("chat.memberTask.sourcesLabel");

  return (
    <>
      <div data-testid="reply-sources" className="mt-2 flex w-full flex-wrap items-center gap-1.5">
        {sources.length > 3 ? (
          <button
            type="button"
            data-testid="reply-sources-summary"
            onClick={() => setOpen(true)}
            className="bg-card border-chat-hairline text-chat-muted hover:border-primary/30 hover:text-chat-ink flex items-center gap-2 rounded-lg border px-2 py-1 text-xs transition-colors"
          >
            <span>
              {label}
              {t("chat.memberTask.sourcesCount").replace("{n}", String(sources.length))}
            </span>
            <span className="flex -space-x-1.5">
              {sources.slice(0, 4).map((source) => (
                <SourceAvatar
                  key={source.url}
                  site={source.site}
                  className="border-background size-5 border"
                />
              ))}
            </span>
          </button>
        ) : (
          sources.map((source) => (
            <a
              key={source.url}
              href={source.url}
              target="_blank"
              rel="noreferrer"
              title={source.title}
              className="bg-card border-chat-hairline text-chat-ink hover:border-primary/40 flex max-w-72 items-center gap-1.5 truncate rounded-lg border px-2 py-1 text-xs"
            >
              <SourceAvatar site={source.site} className="size-4" />
              <span className="truncate">{source.site}</span>
              <LinkIcon className="text-chat-muted size-3 shrink-0" />
            </a>
          ))
        )}
      </div>

      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent
          side="right"
          className="flex w-full flex-col gap-0 p-0 data-[side=right]:sm:max-w-[460px]"
          aria-describedby={undefined}
        >
          <SheetHeader className="border-chat-hairline border-b px-5 py-4">
            <SheetTitle className="text-chat-ink text-sm font-semibold">
              {t("chat.memberTask.sourcesPanelTitle")}
            </SheetTitle>
            <SheetDescription className="text-chat-muted text-[11px]">
              {t("chat.memberTask.sourcesCount").replace("{n}", String(sources.length))}
            </SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-y-auto px-5">
            {sources.map((source) => (
              <a
                key={source.url}
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="border-chat-hairline hover:bg-primary/[0.02] flex gap-3 border-b py-3.5 last:border-b-0"
              >
                <SourceAvatar site={source.site} className="mt-0.5 size-7" />
                <div className="min-w-0 flex-1">
                  <div className="text-chat-muted flex items-center gap-2 text-[11px]">
                    <span className="truncate">{source.site}</span>
                    {source.date ? <span className="shrink-0">{source.date}</span> : null}
                  </div>
                  <p className="text-chat-ink mt-1 line-clamp-2 text-sm leading-5 font-medium">
                    {source.title}
                  </p>
                  {source.snippet ? (
                    <p className="text-chat-muted mt-1 line-clamp-3 text-xs leading-5">
                      {source.snippet}
                    </p>
                  ) : null}
                </div>
              </a>
            ))}
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
