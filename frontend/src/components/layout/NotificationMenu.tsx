/**
 * 功能：消息中心（通知 + 待确认审批）——宽面板左列表右详情。
 * 入口仍在左栏底部铃铛；打开后左缘与铃铛对齐，详情不再使用内联原文。
 */
import { Bell, Trash, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { memoryApprovalApi } from "@/services/approvals";
import { useChatStore } from "@/stores/chat-store";

const POLL_MS = 30_000;

type MemoryPending = Awaited<ReturnType<typeof memoryApprovalApi.list>>["pending"][number];

type Filter = "all" | "long" | "draft" | "notice";

type QueueItem =
  | { key: string; kind: "memory"; item: MemoryPending; memoryKind: "long" | "draft" }
  | { key: string; kind: "notice"; item: ReturnType<typeof useChatStore.getState>["notifications"][number] };

interface ResolvedRow {
  key: string;
  action: "confirmed" | "rejected";
  at: number;
}

const RESOLVED_GRACE_MS = 8_000;

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  return new Date(seconds * 1000).toLocaleString();
}

function firstLine(text: string): string {
  return (text || "").split("\n").find((line) => line.trim())?.trim() ?? "";
}

/** 功能：把后端判词清理成人读文案（去字段前缀/英文术语/代码围栏）。 */
function memoryKind(item: MemoryPending): "long" | "draft" {
  const kind = String(item.draft?.kind ?? "");
  if (kind === "后台直写暂存" || kind === "冲突裁决") return "draft";
  return "long";
}

function memoryTitle(item: MemoryPending): string {
  return firstLine(item.summary).replace(/^内容：\s*/, "").slice(0, 72);
}

function queueId(item: QueueItem): string | undefined {
  if (item.kind === "notice") return item.item.id;
  return item.item.id;
}

function QueueBadge({ tone, children }: { tone: "long" | "draft" | "notice"; children: React.ReactNode }) {
  const styles = {
    long: "border-indigo-200 bg-indigo-50 text-indigo-700",
    draft: "border-slate-200 bg-slate-100 text-slate-600",
    notice: "border-border bg-muted text-muted-foreground",
  }[tone];
  return (
    <span className={cn("inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium", styles)}>
      {children}
    </span>
  );
}

export function NotificationMenu() {
  const t = useT();
  const navigate = useNavigate();
  const notifications = useChatStore((state) => state.notifications);
  const unread = notifications.filter((item) => !item.read).length;
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [memoryPending, setMemoryPending] = useState<MemoryPending[]>([]);
  const [draftCount, setDraftCount] = useState(0);
  const [resolved, setResolved] = useState<ResolvedRow[]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [listExpanded, setListExpanded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [busyId, setBusyId] = useState("");

  const loadPending = useCallback(async () => {
    try {
      const memories = await memoryApprovalApi.list();
      setMemoryPending(memories.pending ?? []);
      setDraftCount(memories.drafts ?? 0);
      setLoadError("");
      const now = Date.now();
      setResolved((previous) => previous.filter((item) => now - item.at < RESOLVED_GRACE_MS));
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  useEffect(() => {
    void loadPending();
  }, [loadPending]);

  useEffect(() => {
    if (!open) return;
    void loadPending();
    const timer = window.setInterval(() => void loadPending(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [open, loadPending]);

  const items = useMemo<QueueItem[]>(() => {
    const result: QueueItem[] = [];
    for (const item of memoryPending) {
      result.push({
        key: `memory:${item.id}`,
        kind: "memory",
        item,
        memoryKind: memoryKind(item),
      });
    }
    for (const item of notifications) {
      result.push({ key: `notice:${item.id}`, kind: "notice", item });
    }
    return result;
  }, [memoryPending, notifications]);

  const filtered = useMemo(() => {
    if (filter === "all") return items;
    if (filter === "long") return items.filter((item) => item.kind === "memory" && item.memoryKind === "long");
    if (filter === "draft") return items.filter((item) => item.kind === "memory" && item.memoryKind === "draft");
    return items.filter((item) => item.kind === "notice");
  }, [filter, items]);

  const resolvedKeys = new Set(resolved.map((item) => item.key));
  const pendingVisible = filtered.filter((item) => !resolvedKeys.has(item.key));
  const visible = filter === "all" && !listExpanded ? pendingVisible.slice(0, 5) : pendingVisible;

  useEffect(() => {
    if (!open) return;
    if (!filtered.some((item) => item.key === selectedKey)) {
      setSelectedKey(filtered[0]?.key ?? "");
    }
  }, [filtered, open, selectedKey]);

  const selected = items.find((item) => item.key === selectedKey) ?? null;
  const pendingCount = memoryPending.length;
  const badge = unread + pendingCount;

  const markResolved = (key: string, action: "confirmed" | "rejected") => {
    setResolved((previous) => [
      ...previous.filter((item) => item.key !== key),
      { key, action, at: Date.now() },
    ]);
    setSelectedKey("");
  };

  const runMemory = async (item: MemoryPending, approve: boolean) => {
    setBusyId(item.id);
    try {
      if (approve) await memoryApprovalApi.approve(item.id);
      else await memoryApprovalApi.reject(item.id);
      markResolved(`memory:${item.id}`, approve ? "confirmed" : "rejected");
      await loadPending();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setLoadError(message);
      toast.error(message);
    } finally {
      setBusyId("");
    }
  };

  const changeFilter = (next: Filter) => {
    setFilter(next);
    setListExpanded(false);
    const first = items.find((item) => {
      if (next === "all") return true;
      if (next === "long") return item.kind === "memory" && item.memoryKind === "long";
      if (next === "draft") return item.kind === "memory" && item.memoryKind === "draft";
      return item.kind === "notice";
    });
    setSelectedKey(first?.key ?? "");
  };

  const renderMemoryDetail = (item: MemoryPending) => {
    const isDraft = memoryKind(item) === "draft";
    const target = String(item.draft?.target ?? (isDraft ? "长期记忆" : "长期记忆"));
    const origin = item.origin === "user_like" ? t("notify.source.userLike") : t("notify.source.auto");
    const lines = (item.summary || "").split("\n").filter((line) => line.trim());
    return (
      <div data-testid="approval-detail-personal" className="space-y-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <QueueBadge tone={isDraft ? "draft" : "long"}>
            {isDraft ? t("notify.kind.draft") : t("notify.kind.nominated")}
          </QueueBadge>
          {item.conflict ? (
            <span className="rounded-full border border-red-200 bg-red-50 px-1.5 py-0.5 text-[10px] text-red-700">
              {t("notify.conflictBadge")}
            </span>
          ) : null}
        </div>
        {item.conflict ? (
          <section>
            <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">与旧记忆对照</h4>
            <div className="rounded-xl border border-red-100 bg-red-50/40 px-3 py-2 text-[11px]">
              <p className="text-muted-foreground">旧值（已落库）</p>
              <p className="mt-1 whitespace-pre-wrap" data-testid="approval-diff-old">
                {item.diff?.old || item.old_value || "（未取到原记忆）"}
              </p>
              <p className="text-muted-foreground mt-2">新值（批准后覆盖）</p>
              <p className="mt-1 whitespace-pre-wrap" data-testid="approval-diff-new">
                {item.diff?.new || item.content || lines.join("\n")}
              </p>
            </div>
          </section>
        ) : null}
        <section>
          <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">
            {isDraft ? "草稿内容" : "记忆内容"}
          </h4>
          <p
            data-testid="approval-detail-content"
            className="rounded-xl border border-border/70 bg-muted/40 px-3 py-2.5 text-[11px] leading-relaxed whitespace-pre-wrap"
          >
            {item.content || lines.join("\n")}
          </p>
        </section>
        <section className="rounded-xl border border-border/70 px-3 py-2.5">
          <p className="text-[11px]">
            <span className="text-muted-foreground">写入目标：</span>
            {target}
          </p>
          <p className="mt-1 text-[11px]">
            <span className="text-muted-foreground">来源：</span>
            {origin}
          </p>
          {isDraft ? (
            <p className="mt-1 text-[11px] text-muted-foreground">
              批准后草稿转为正式记忆，才会进入召回。
            </p>
          ) : null}
        </section>

      </div>
    );
  };

  const renderActions = () => {
    if (!selected || selected.kind !== "memory") return null;
    const draft = memoryKind(selected.item) === "draft";
    return (
      <>
        <Button
          variant="outline"
          size="sm"
          disabled={busyId === selected.item.id}
          data-testid="approval-reject-personal"
          onClick={() => void runMemory(selected.item, false)}
        >
          {t("notify.reject")}
        </Button>
        <Button
          size="sm"
          disabled={busyId === selected.item.id}
          data-testid="approval-approve-personal"
          onClick={() => void runMemory(selected.item, true)}
        >
          {draft ? "批准转正" : "批准写入"}
        </Button>
      </>
    );
  };

  const renderDetail = () => {
    if (!selected) {
      return (
        <div className="flex h-full items-center justify-center px-8 text-center text-xs text-muted-foreground">
          选择左侧一条记录查看详情
        </div>
      );
    }
    if (selected.kind === "memory") return renderMemoryDetail(selected.item);
    return (
      <div className="space-y-4">
        <QueueBadge tone="notice">通知</QueueBadge>
        <h3 className="text-base font-semibold">{selected.item.title}</h3>
        {selected.item.body ? (
          <p className="text-muted-foreground text-[11px] leading-relaxed whitespace-pre-wrap">
            {selected.item.body}
          </p>
        ) : null}
        {selected.item.sessionId ? (
          <Button
            size="sm"
            onClick={() => {
              useChatStore.getState().setActiveSession(selected.item.sessionId!);
              setOpen(false);
              navigate("/");
            }}
          >
            打开会话
          </Button>
        ) : null}
      </div>
    );
  };

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: "全部" },
    { key: "long", label: "长期记忆" },
    { key: "draft", label: "草稿记忆" },
    { key: "notice", label: "通知" },
  ];

  return (
    <Popover open={open} onOpenChange={(next) => {
      setOpen(next);
      if (next) useChatStore.getState().markNotificationsRead();
    }}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          title={t("header.notifications")}
          className="no-focus-ring relative"
          data-testid="notification-trigger"
        >
          <Bell strokeWidth={1.75} />
          {badge > 0 ? (
            <span className="bg-destructive text-destructive-foreground absolute -top-0.5 -right-0.5 flex size-4 items-center justify-center rounded-full text-[9px] font-medium">
              {badge > 9 ? "9+" : badge}
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        sideOffset={8}
        role="region"
        aria-label={t("header.notifications")}
        className="flex h-[min(660px,calc(100vh-90px))] w-[min(740px,calc(100vw-24px))] flex-col gap-0 overflow-hidden rounded-2xl border border-border bg-card p-0 shadow-xl"
      >
        <header className="flex items-center justify-between px-4 py-3">
          <h2 className="text-[15px] font-semibold">消息中心</h2>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="关闭"
            data-testid="notification-close"
            onClick={() => setOpen(false)}
          >
            <X className="size-3.5" />
          </Button>
        </header>

        <div
          className="flex items-center gap-2 border-b border-border px-4 py-2"
          data-testid="notification-filter-row"
        >
          <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
            {filters.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => changeFilter(item.key)}
                className={cn(
                  "shrink-0 rounded-full border border-transparent px-2.5 py-1 text-[10.5px] text-muted-foreground transition-colors",
                  filter === item.key && "border-transparent bg-foreground font-medium text-background",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-muted-foreground hover:text-foreground shrink-0"
            title={t("header.clearNotifications")}
            data-testid="clear-notifications"
            disabled={notifications.length === 0}
            onClick={() => {
              useChatStore.getState().clearNotifications();
              setSelectedKey("");
            }}
          >
            <Trash className="size-3.5" />
          </Button>
        </div>

        {loadError ? (
          <p className="border-b border-border px-4 py-2 text-[11px] text-destructive" role="alert">
            {loadError}
          </p>
        ) : null}

        {draftCount > 0 ? (
          <p
            className="border-b border-border px-4 py-1.5 text-[10.5px] text-muted-foreground"
            data-testid="draft-pool-hint"
          >
            草稿池还有 {draftCount} 条待晋级：攒满容量或等满一天，才会挑最有价值的进审批
          </p>
        ) : null}

        <div className="flex min-h-0 flex-1">
          <aside className="w-[218px] shrink-0 border-r border-border p-1.5">
            <ScrollArea className="h-full">
            {filtered.length === 0 ? (
              <p className="px-2 py-6 text-center text-[11px] text-muted-foreground">
                {filter === "all" ? "暂无通知" : "暂无内容"}
              </p>
            ) : null}
            {visible.map((item) => {
              const isResolved = resolvedKeys.has(item.key);
              const title = item.kind === "memory" ? memoryTitle(item.item) : item.item.title;
              const meta =
                item.kind === "memory"
                  ? [
                      item.memoryKind === "draft" ? "后台直写暂存" : "长期记忆",
                      formatTime(item.item.created_at ?? 0),
                    ]
                  : [formatTime(item.item.ts), ""];
              const tone = item.kind === "memory" ? item.memoryKind : "notice";
              const testId = item.kind === "memory" ? "approval-row-personal" : "notification-row";
              return (
                <button
                  key={item.key}
                  type="button"
                  data-testid={testId}
                  data-approval-id={item.kind === "notice" ? undefined : queueId(item)}
                  aria-current={selectedKey === item.key}
                  onClick={() => setSelectedKey(item.key)}
                  className={cn(
                    "mb-1 block w-full rounded-xl px-2.5 py-2 text-left transition-colors hover:bg-muted/60",
                    selectedKey === item.key && "bg-muted",
                    isResolved && "opacity-50",
                  )}
                >
                  <div className="flex items-center gap-1.5">
                    <QueueBadge tone={tone}>
                      {item.kind === "memory"
                        ? item.memoryKind === "draft"
                          ? "草稿记忆"
                          : "长期记忆"
                        : "通知"}
                    </QueueBadge>
                    {isResolved ? (
                      <span className="text-[10px] text-muted-foreground">
                        {resolved.find((row) => row.key === item.key)?.action === "confirmed" ? "已批准" : "已驳回"}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1 line-clamp-2 text-[11.5px] font-medium leading-snug">
                    {title}
                  </div>
                  <div className="mt-1 truncate text-[10px] text-muted-foreground">
                    {meta.filter(Boolean).join(" · ")}
                  </div>
                </button>
              );
            })}
            {filter === "all" && filtered.length > 5 ? (
              <button
                type="button"
                data-testid="approval-show-all"
                className="flex w-full items-center gap-0.5 px-2 py-1.5 text-[10px] text-muted-foreground hover:text-foreground"
                onClick={() => setListExpanded((value) => !value)}
              >
                {listExpanded ? "收起" : `查看更多（${filtered.length - 5}）`}
              </button>
            ) : null}
            {resolved.map((item) => (
              <p
                key={item.key}
                data-testid="approval-resolved"
                className="px-2 py-1 text-[10px] text-muted-foreground"
              >
                {item.action === "confirmed" ? "已批准" : "已驳回"}
              </p>
            ))}
            </ScrollArea>
          </aside>

          <section className="min-w-0 flex-1">
            <ScrollArea className="h-full">
              <div className="px-5 py-4">{renderDetail()}</div>
            </ScrollArea>
          </section>
        </div>
        {selected && selected.kind !== "notice" ? (
          <footer className="flex items-center justify-between gap-3 border-t border-border px-4 py-3">
            <p className="text-muted-foreground text-[10px] leading-relaxed">
              批准后写入长期记忆或草稿转正；不会自动修改员工的人设与权限。
            </p>
            <div className="flex shrink-0 items-center gap-2">{renderActions()}</div>
          </footer>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}