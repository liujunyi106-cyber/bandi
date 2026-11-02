/**
 * 功能：知识库引用 chips——回答下方展示 knowledge_search.citations，点击展开引用原文。
 */
import { BookOpen } from "lucide-react";
import { useMemo, useState } from "react";

import { useT } from "@/i18n";
import type { CitationRef } from "@/stores/chat-store";

const MAX_VISIBLE = 4;

function citationKey(citation: CitationRef, index: number): string {
  return citation.id || `${citation.source_path || ""}:${citation.chunk_id || ""}:${index}`;
}

export function CitationChips({ citations }: { citations: CitationRef[] }) {
  const t = useT();
  const [activeKey, setActiveKey] = useState("");

  const visible = useMemo(() => {
    const seen = new Set<string>();
    const out: CitationRef[] = [];
    for (const citation of citations) {
      const key = citationKey(citation, out.length);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(citation);
      if (out.length >= MAX_VISIBLE) break;
    }
    return out;
  }, [citations]);

  if (visible.length === 0) return null;

  const active = visible.find((citation, index) => citationKey(citation, index) === activeKey);

  return (
    <div className="mt-2 flex w-full flex-col gap-1.5">
      <div data-testid="citation-chips" className="flex w-full flex-wrap items-center gap-1.5">
        {visible.map((citation, index) => {
          const key = citationKey(citation, index);
          const title = String(
            citation.title || citation.source_path || citation.chunk_id || "知识库引用",
          );
          const selected = key === activeKey;
          return (
            <button
              key={key}
              type="button"
              data-testid="citation-chip"
              aria-pressed={selected}
              className={`bg-card border-chat-hairline text-chat-muted hover:border-primary/30 hover:text-chat-ink flex max-w-[20rem] items-center gap-1 rounded-lg border px-2 py-1 text-xs transition-colors ${
                selected ? "border-primary/40 text-chat-ink" : ""
              }`}
              title={citation.source_path || title}
              onClick={() => setActiveKey(selected ? "" : key)}
            >
              <BookOpen className="size-3.5 shrink-0" />
              <span className="shrink-0 tabular-nums">
                {citation.label || `[${index + 1}]`}
              </span>
              <span className="truncate">{title}</span>
            </button>
          );
        })}
      </div>
      {active ? (
        <div
          data-testid="citation-detail"
          className="bg-card border-chat-hairline w-full rounded-lg border p-2 text-xs"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-chat-ink font-medium">
                {active.label ? `${active.label} ` : ""}
                {active.title || active.source_path || "知识库引用"}
              </p>
              {active.source_path ? (
                <p className="text-chat-muted mt-0.5 break-all text-[11px]">
                  {active.source_path}
                </p>
              ) : null}
            </div>
            <button
              type="button"
              className="text-chat-muted hover:text-chat-ink shrink-0 text-[11px]"
              onClick={() => setActiveKey("")}
            >
              {t("common.close")}
            </button>
          </div>
          <p className="text-chat-muted mt-1.5 max-h-40 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed">
            {active.excerpt || active.content || active.summary || ""}
          </p>
        </div>
      ) : null}
    </div>
  );
}
