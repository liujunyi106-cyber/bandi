/**
 * 功能：知识库页——页头 + 页签工具栏（文档资料 / 图谱）+ 共享搜索词。
 * 页签与工具栏布局对齐团队页：标题/副标题一行，下面 `SegmentedTabs` + 右侧搜索。
 */
import { Search } from "lucide-react";
import { useState } from "react";

import { Input } from "@/components/ui/input";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { useT } from "@/i18n";
import { useKnowledgeStore } from "@/stores/knowledge-store";

import { DocumentsTab } from "./DocumentsTab";
import { GraphTab } from "./GraphTab";

type KnowledgeTab = "docs" | "graph";

export default function KnowledgePage() {
  const t = useT();
  const [tab, setTab] = useState<KnowledgeTab>("docs");
  const query = useKnowledgeStore((store) => store.query);
  const setQuery = useKnowledgeStore((store) => store.setQuery);

  const items = [
    { value: "docs", label: t("knowledge.tabDocs") },
    { value: "graph", label: t("knowledge.tabGraph") },
  ];

  return (
    <div className="mx-auto flex h-full min-h-0 w-full max-w-[1400px] flex-col px-8 pt-9 pb-7">
      <header className="shrink-0">
        <h1 className="text-chat-ink text-xl font-semibold">{t("knowledge.title")}</h1>
        <p className="text-chat-muted mt-1 text-xs">{t("knowledge.subtitle")}</p>
      </header>

      <div className="mt-6 flex min-h-9 shrink-0 flex-wrap items-center justify-between gap-3">
        <SegmentedTabs
          ariaLabel={t("knowledge.title")}
          items={items}
          onChange={(value) => setTab(value as KnowledgeTab)}
          value={tab}
        />
        <label className="border-chat-hairline bg-card flex h-9 w-[260px] items-center gap-2 rounded-xl border px-3">
          <Search className="text-chat-muted size-4 shrink-0" aria-hidden="true" />
          <Input
            className="h-full min-w-0 flex-1 border-0 bg-transparent px-0 text-xs shadow-none"
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t("knowledge.searchPlaceholder")}
            value={query}
          />
        </label>
      </div>

      <div className="mt-4 min-h-0 flex-1">
        {tab === "docs" ? <DocumentsTab query={query} /> : <GraphTab query={query} />}
      </div>
    </div>
  );
}
