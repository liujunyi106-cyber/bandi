/**
 * 功能：会话工具栏（对话内搜索 + 历史提问查询—— ）。
 * 顺序：搜索在前、历史在后；无边框 ghost 图标，与会话标题垂直居中。
 * 对话内搜索：点击 icon 后**向左展开为搜索框**，关键词过滤
 * 当前会话消息，点击定位；历史提问：列出当前会话的用户提问（带标题与浅色分割线），点击定位；
 * 定位依赖消息元素 `data-chat-anchor-key={id}`（ChatView/DmChatView 均已标注）。
 */
import { History, Search, X } from "lucide-react";
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/i18n";

export interface SessionToolItem {
  id: string;
  role: string;
  content: string;
}

/** 功能：滚动定位到消息锚点（平滑滚动到视口中心）。 */
function scrollToAnchor(id: string): void {
  const el = document.querySelector(`[data-chat-anchor-key="${CSS.escape(id)}"]`);
  el?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function preview(text: string, max = 48): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

export function SessionTools({ items, testId }: { items: SessionToolItem[]; testId: string }) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const userAsks = useMemo(() => items.filter((item) => item.role === "user"), [items]);
  const matched = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    return items.filter((item) => item.content.toLowerCase().includes(q)).slice(0, 30);
  }, [items, query]);

  const closeSearch = () => {
    setQuery("");
    setSearchOpen(false);
  };

  return (
    <div className="relative flex items-center gap-1" data-testid={testId}>
      {/* 对话内搜索：icon 点击后向左展开为搜索框（搜索在前，无边框） */}
      {searchOpen ? (
        <div
          data-testid={`${testId}-search-box`}
          className="border-chat-hairline bg-card flex h-8 w-56 items-center gap-1.5 rounded-xl border px-2.5"
        >
          <Search className="text-chat-muted size-3.5 shrink-0" />
          <input
            autoFocus
            data-testid={`${testId}-search-input`}
            className="text-chat-ink placeholder:text-chat-muted min-w-0 flex-1 bg-transparent text-xs outline-none"
            placeholder={t("chat.tools.searchPlaceholder")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") closeSearch();
            }}
          />
          <button
            type="button"
            data-testid={`${testId}-search-close`}
            className="text-chat-muted hover:text-foreground flex size-4 shrink-0 items-center justify-center"
            title={t("common.cancel")}
            onClick={closeSearch}
          >
            <X className="size-3" />
          </button>
        </div>
      ) : (
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground hover:bg-primary/5 hover:text-accent-foreground aria-expanded:bg-primary/5 aria-expanded:text-accent-foreground size-8 rounded-[8px]"
          title={t("chat.tools.search")}
          data-testid={`${testId}-search`}
          onClick={() => setSearchOpen(true)}
        >
          <Search className="size-4" />
        </Button>
      )}

      {/* 搜索结果（展开且有关键词时浮于下方） */}
      {searchOpen && query.trim() ? (
        <div
          data-testid={`${testId}-search-results`}
          className="border-chat-hairline bg-card absolute top-full right-0 z-50 mt-1 max-h-60 w-80 overflow-y-auto rounded-lg border p-1"
        >
          {matched.length === 0 ? (
            <p className="text-muted-foreground py-3 text-center text-xs">
              {t("chat.tools.noResult")}
            </p>
          ) : (
            matched.map((item) => (
              <button
                key={item.id}
                type="button"
                data-testid={`${testId}-search-item`}
                className="hover:bg-primary/5 flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs"
                onClick={() => {
                  scrollToAnchor(item.id);
                  closeSearch();
                }}
              >
                <span className="text-muted-foreground mt-0.5 shrink-0 text-[10px]">
                  {item.role === "user" ? t("chat.group.you") : item.role}
                </span>
                <span className="min-w-0 flex-1 truncate">{preview(item.content, 60)}</span>
              </button>
            ))
          )}
        </div>
      ) : null}

      {/* 历史提问查询（历史在后，无边框；列表带标题与浅色分割线） */}
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:bg-primary/5 hover:text-accent-foreground aria-expanded:bg-primary/5 aria-expanded:text-accent-foreground size-8 rounded-[8px]"
            title={t("chat.tools.history")}
            data-testid={`${testId}-history`}
          >
            <History className="size-4" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="max-h-72 w-80 overflow-y-auto p-1">
          {/*标题左对齐（与下方记录字段对齐）且用浅灰色；列表项不带 icon */}
          <p className="text-muted-foreground px-2 py-1.5 text-sm font-medium">
            {t("chat.tools.history")}
          </p>
          <div className="bg-chat-hairline mx-2 mb-1 h-px" />
          {userAsks.length === 0 ? (
            <p className="text-muted-foreground px-2 py-3 text-center text-xs">
              {t("chat.tools.noResult")}
            </p>
          ) : (
            userAsks.map((item) => (
              <button
                key={item.id}
                type="button"
                data-testid={`${testId}-history-item`}
                className="text-muted-foreground hover:bg-primary/5 hover:text-foreground flex w-full items-start rounded-md px-2 py-1 text-left text-xs"
                onClick={() => scrollToAnchor(item.id)}
              >
                <span className="min-w-0 flex-1 truncate">{preview(item.content)}</span>
              </button>
            ))
          )}
        </PopoverContent>
      </Popover>
    </div>
  );
}
