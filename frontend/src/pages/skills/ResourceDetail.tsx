/**
 * 功能：资源详情（技能 / SOP / 知识文档）——渲染/源码切换、编辑保存、卸载与未保存确认。
 */
import { ArrowLeft, Code2, Eye, FolderOpen, MoreHorizontal, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Switch } from "@/components/ui/switch";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { resourcesApi, type ResourceItem, type ResourceKind } from "@/services/resources";

function stripFrontmatter(text: string): string {
  if (!text.startsWith("---")) return text;
  const end = text.indexOf("\n---", 3);
  return end === -1 ? text : text.slice(end + 4).replace(/^\s+/, "");
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

interface SopCardShape {
  name?: string;
  skill_id?: string;
  description?: string;
  goal?: string[];
  nodes?: Array<{ name?: string; node_id?: string; instruction?: string }>;
}

function SopPreview({ content }: { content: string }) {
  const t = useT();
  const card = useMemo<SopCardShape | null>(() => {
    try {
      const parsed = JSON.parse(content);
      return parsed && typeof parsed === "object" ? (parsed as SopCardShape) : null;
    } catch {
      return null;
    }
  }, [content]);
  if (!card) {
    return (
      <p className="text-destructive rounded-lg border border-[#f0d7d3] bg-[#fff8f7] px-4 py-3 text-xs">
        {t("skillsMarket.detail.invalidSop")}
      </p>
    );
  }
  const goals = Array.isArray(card.goal) ? card.goal : [];
  const nodes = Array.isArray(card.nodes) ? card.nodes : [];
  return (
    <div className="mx-auto max-w-[860px]">
      <h1 className="text-chat-ink text-xl font-semibold tracking-tight">
        {card.name || card.skill_id || t("skillsMarket.detail.untitled")}
      </h1>
      {card.description ? (
        <p className="text-chat-muted mt-2 text-xs leading-5">{card.description}</p>
      ) : null}
      {goals.length > 0 ? (
        <>
          <h2 className="text-chat-ink mt-5 text-[13px] font-semibold">{t("skillsMarket.detail.goal")}</h2>
          <ul className="text-chat-ink mt-2 list-disc space-y-1 pl-5 text-xs leading-5">
            {goals.map((goal, index) => (
              <li key={index}>{goal}</li>
            ))}
          </ul>
        </>
      ) : null}
      <h2 className="text-chat-ink mt-5 text-[13px] font-semibold">
        {t("skillsMarket.detail.nodes")}
      </h2>
      <ol className="mt-2 flex flex-col gap-2">
        {nodes.map((node, index) => (
          <li
            key={node.node_id || index}
            className="border-chat-hairline bg-chat-bg flex gap-3 rounded-lg border px-3 py-2.5"
          >
            <span className="text-chat-muted bg-primary/5 mt-0.5 grid size-5 shrink-0 place-items-center rounded-md text-[10px] font-semibold">
              {index + 1}
            </span>
            <div className="min-w-0">
              <strong className="text-chat-ink block text-xs font-semibold">
                {node.name || node.node_id || t("skillsMarket.detail.untitledNode")}
              </strong>
              <p className="text-chat-muted mt-1 text-[11px] leading-5">
                {node.instruction || t("skillsMarket.detail.emptyNode")}
              </p>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

export interface ResourceDetailProps {
  kind: ResourceKind;
  resourceId: string;
  onBack: () => void;
  onRemoved: () => void;
  onEdit?: () => void;
}

/** 功能：资源详情页（覆盖列表区域）。 */
export function ResourceDetail({
  kind,
  resourceId,
  onBack,
  onRemoved,
  onEdit,
}: ResourceDetailProps) {
  const t = useT();
  const [item, setItem] = useState<ResourceItem | null>(null);
  const [content, setContent] = useState("");
  const [saved, setSaved] = useState("");
  const [mode, setMode] = useState<"preview" | "source">("preview");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [unsavedOpen, setUnsavedOpen] = useState(false);
  const [uninstallOpen, setUninstallOpen] = useState(false);
  const sourceRef = useRef<HTMLTextAreaElement>(null);
  const undoRef = useRef<string[]>([]);
  const lastValueRef = useRef("");
  const lastEditAtRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    resourcesApi
      .detail(kind, resourceId)
      .then((payload) => {
        if (cancelled) return;
        setItem(payload.item);
        setContent(payload.content);
        setSaved(payload.content);
        lastValueRef.current = payload.content;
        lastEditAtRef.current = 0;
        undoRef.current = [];
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(errorText(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, resourceId]);

  const dirty = content !== saved;

  const save = useCallback(async (): Promise<boolean> => {
    if (!item || saving) return false;
    setSaving(true);
    try {
      const result = await resourcesApi.save(kind, item.id, content);
      setItem(result.item);
      setSaved(content);
      lastEditAtRef.current = 0;
      toast.success(t("skillsMarket.detail.saveOk"));
      return true;
    } catch (reason) {
      toast.error(`${t("skillsMarket.detail.saveFailed")}：${errorText(reason)}`);
      return false;
    } finally {
      setSaving(false);
    }
  }, [content, item, kind, saving, t]);

  const undo = useCallback(() => {
    const previous = undoRef.current.pop();
    if (previous === undefined) return;
    setContent(previous);
    lastValueRef.current = previous;
    lastEditAtRef.current = 0;
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key === "s") {
        event.preventDefault();
        void save();
        return;
      }
      if (key === "z" && mode === "source" && document.activeElement === sourceRef.current) {
        event.preventDefault();
        undo();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [mode, save, undo]);

  const toggle = async (enabled: boolean) => {
    if (!item) return;
    setBusy(true);
    try {
      const result = await resourcesApi.setEnabled(kind, item.id, enabled);
      setItem(result.item);
      toast.success(
        enabled
          ? t("skillsMarket.toast.enabled").replace("{name}", item.title)
          : t("skillsMarket.toast.disabled").replace("{name}", item.title),
      );
    } catch (reason) {
      toast.error(`${t("skillsMarket.toast.toggleFailed")}：${errorText(reason)}`);
    } finally {
      setBusy(false);
    }
  };

  const openFolder = async () => {
    if (!item) return;
    try {
      await resourcesApi.openFolder(kind, item.id);
      toast.success(t("skillsMarket.toast.folderOpened"));
    } catch (reason) {
      toast.error(`${t("skillsMarket.toast.folderFailed")}：${errorText(reason)}`);
    }
  };

  const uninstall = async () => {
    if (!item) return;
    setBusy(true);
    try {
      await resourcesApi.uninstall(kind, item.id);
      toast.success(t("skillsMarket.toast.uninstalled").replace("{name}", item.title));
      setUninstallOpen(false);
      onRemoved();
    } catch (reason) {
      toast.error(`${t("skillsMarket.toast.uninstallFailed")}：${errorText(reason)}`);
    } finally {
      setBusy(false);
    }
  };

  const requestBack = () => {
    if (dirty) {
      setUnsavedOpen(true);
      return;
    }
    onBack();
  };

  const handleSourceChange = (next: string) => {
    const now = Date.now();
    if (now - lastEditAtRef.current > 600) {
      undoRef.current.push(lastValueRef.current);
      if (undoRef.current.length > 200) undoRef.current.shift();
    }
    lastEditAtRef.current = now;
    lastValueRef.current = next;
    setContent(next);
  };

  if (loading) {
    return (
      <div className="text-chat-muted flex h-full items-center justify-center text-xs">
        {t("skillsMarket.loading")}
      </div>
    );
  }
  if (error || !item) {
    return (
      <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-4 px-8 pt-9">
        <Button variant="ghost" size="sm" className="text-chat-muted -ml-2" onClick={onBack}>
          <ArrowLeft />
          {t("skillsMarket.detail.back")}
        </Button>
        <p className="text-destructive text-xs">{error || t("skillsMarket.loadFailed")}</p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mx-auto flex min-h-0 w-full max-w-[1400px] flex-1 flex-col px-8 pt-9 pb-7">
        <div className="flex shrink-0 items-center">
          <Button variant="ghost" size="sm" className="text-chat-muted -ml-2" onClick={requestBack}>
            <ArrowLeft />
            {t("skillsMarket.detail.back")}
          </Button>
        </div>
        <div className="mt-4 flex shrink-0 flex-wrap items-center gap-3">
          <h1 className="text-chat-ink min-w-0 text-xl font-semibold tracking-tight">
            {item.title}
          </h1>
          <Switch
            checked={item.enabled}
            disabled={busy}
            aria-label={t("skillsMarket.detail.toggle")}
            onCheckedChange={(next) => void toggle(next)}
          />
          {kind === "sop" && onEdit ? (
            <Button variant="outline" size="sm" onClick={onEdit}>
              可视化编辑
            </Button>
          ) : null}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon-sm" aria-label={t("skillsMarket.detail.more")}>
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-40">
              <DropdownMenuItem onSelect={() => void openFolder()}>
                <FolderOpen />
                {t("skillsMarket.detail.openFolder")}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem variant="destructive" onSelect={() => setUninstallOpen(true)}>
                <Trash2 />
                {t("skillsMarket.detail.uninstall")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {item.description ? (
          <p className="text-chat-muted mt-1.5 line-clamp-3 max-w-[820px] shrink-0 text-xs leading-5">
            {item.description}
          </p>
        ) : null}

        <div className="border-chat-hairline bg-card mt-4 flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border">
          <div className="border-chat-hairline bg-chat-bg flex h-11 shrink-0 items-center gap-3 border-b px-3">
            <span className="text-chat-muted min-w-0 flex-1 truncate font-mono text-[11px]">
              {item.path}
            </span>
            <div className="flex shrink-0 items-center gap-1">
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={t("skillsMarket.detail.preview")}
                className={cn(mode === "preview" && "bg-primary/5")}
                onClick={() => setMode("preview")}
              >
                <Eye />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={t("skillsMarket.detail.source")}
                className={cn(mode === "source" && "bg-primary/5")}
                onClick={() => setMode("source")}
              >
                <Code2 />
              </Button>
            </div>
          </div>
          {mode === "preview" ? (
            <div className="min-h-0 flex-1 overflow-auto px-7 py-6">
              {kind === "sop" ? (
                <SopPreview content={content} />
              ) : (
                <div className="mx-auto max-w-[860px]">
                  <MarkdownRenderer text={stripFrontmatter(content)} />
                </div>
              )}
            </div>
          ) : (
            <textarea
              ref={sourceRef}
              value={content}
              spellCheck={false}
              aria-label={t("skillsMarket.detail.sourceLabel")}
              onChange={(event) => handleSourceChange(event.target.value)}
              className="text-chat-ink min-h-0 flex-1 resize-none bg-transparent px-5 py-4 font-mono text-xs leading-6 outline-none"
            />
          )}
          <div className="border-chat-hairline bg-chat-bg text-chat-muted flex h-8 shrink-0 items-center justify-between gap-3 border-t px-3 text-[11px]">
            <span className={cn(dirty && "text-amber-700")}>
              {dirty ? t("skillsMarket.detail.dirty") : t("skillsMarket.detail.saved")}
            </span>
            <span>{t("skillsMarket.detail.shortcuts")}</span>
          </div>
        </div>
      </div>

      <AlertDialog open={unsavedOpen} onOpenChange={setUnsavedOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("skillsMarket.detail.unsavedTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("skillsMarket.detail.unsavedDesc")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              onClick={() => {
                setUnsavedOpen(false);
                onBack();
              }}
            >
              {t("common.cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                void save().then((ok) => {
                  if (ok) {
                    setUnsavedOpen(false);
                    onBack();
                  }
                });
              }}
            >
              {t("skillsMarket.detail.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={uninstallOpen} onOpenChange={setUninstallOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t("skillsMarket.detail.uninstallTitle").replace("{name}", item.title)}
            </AlertDialogTitle>
            <AlertDialogDescription>{t("skillsMarket.detail.uninstallDesc")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={busy}
              onClick={(event) => {
                event.preventDefault();
                void uninstall();
              }}
            >
              {t("skillsMarket.detail.uninstall")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
