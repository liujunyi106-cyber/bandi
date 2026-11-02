/**
 * 功能：知识库「文档资料」页签——左树 + 右侧详情；新建/上传/链接/改名/移动/员工/删除。
 * 交互：搜索 200ms 防抖、键盘树导航、宽度拖拽、删除二次确认带数量、上传进度可取消。
 */
import { Download, Menu, MoreHorizontal } from "lucide-react";
import { type ChangeEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

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
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useWindowWidth } from "@/hooks/use-window-width";
import { useT } from "@/i18n";
import { companyApi, type EmployeeSummary } from "@/services/company";
import { ApiError } from "@/services/api";
import {
  knowledgeNodesApi,
  type KnowledgeNode,
  type KnowledgeNodeContent,
} from "@/services/knowledge-nodes";
import { describeError } from "@/services/team";

import { CsvTable, type CsvViewState } from "./CsvTable";
import { MarkdownEditor } from "./MarkdownEditor";
import { NodeBody } from "./NodeBody";
import { NodeTree } from "./NodeTree";
import { PptxView } from "./PptxView";
import { XlsxView } from "./XlsxView";

const TREE_WIDTH_KEY = "bandi.knowledge.treeWidth";
const TREE_WIDTH_DEFAULT = 288;
const SEARCH_DEBOUNCE_MS = 200;
const INGEST_IDLE_MS = 5000;

type CreateKind = "folder" | "md" | "csv" | "link";

interface CreateState {
  parentId: string;
  kind: CreateKind;
  name: string;
  url: string;
}

export function DocumentsTab({ query }: { query: string }) {
  const t = useT();
  const windowWidth = useWindowWidth();
  const mobile = windowWidth < 1024;

  const [baseId, setBaseId] = useState("");
  const [nodes, setNodes] = useState<KnowledgeNode[]>([]);
  const [selected, setSelected] = useState<KnowledgeNode | null>(null);
  const [content, setContent] = useState<KnowledgeNodeContent | null>(null);
  const [csvView, setCsvView] = useState<CsvViewState>({});
  const [search, setSearch] = useState(query);
  const [debounced, setDebounced] = useState(query);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const [creating, setCreating] = useState<CreateState | null>(null);
  const [moving, setMoving] = useState<{ node: KnowledgeNode; folders: KnowledgeNode[] } | null>(
    null,
  );
  const [assigning, setAssigning] = useState<{
    node: KnowledgeNode;
    employees: EmployeeSummary[];
  } | null>(null);
  const [deleting, setDeleting] = useState<KnowledgeNode | null>(null);
  const [uploading, setUploading] = useState<{
    name: string;
    ratio: number;
    abort: () => void;
  } | null>(null);
  const [error, setError] = useState("");
  const [editorVersion, setEditorVersion] = useState(0);
  const [snapshotting, setSnapshotting] = useState(false);
  const [width, setWidth] = useState(() => {
    if (typeof window === "undefined") return TREE_WIDTH_DEFAULT;
    const stored = Number(window.localStorage.getItem(TREE_WIDTH_KEY));
    return stored >= 240 && stored <= 480 ? stored : TREE_WIDTH_DEFAULT;
  });
  const [mobileOpen, setMobileOpen] = useState(false);

  const fileInput = useRef<HTMLInputElement | null>(null);
  const uploadTarget = useRef("");
  const mtimeRef = useRef<Map<string, number>>(new Map());
  const ingestTimers = useRef<Map<string, number>>(new Map());
  const lastNodeId = useRef("");
  const selectedRef = useRef<KnowledgeNode | null>(null);
  const conflictVersions = useRef<Map<string, KnowledgeNodeContent>>(new Map());

  selectedRef.current = selected;

  useEffect(() => setSearch(query), [query]);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(search), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    window.localStorage.setItem(TREE_WIDTH_KEY, String(width));
  }, [width]);

  const refresh = useCallback(
    async (id: string) => {
      if (!id) return;
      try {
        const list = await knowledgeNodesApi.listAll(id);
        setNodes(list);
      } catch (cause) {
        setError(describeError(cause, t("knowledge.loadFailed")));
      }
    },
    [t],
  );

  useEffect(() => {
    let cancelled = false;
    knowledgeNodesApi
      .bases()
      .then((bases) => {
        if (cancelled) return;
        setBaseId(bases[0]?.base_id ?? "default");
      })
      .catch((cause) => {
        if (cancelled) return;
        setBaseId("default");
        setError(describeError(cause, t("knowledge.loadFailed")));
      });
    return () => {
      cancelled = true;
    };
  }, [t]);

  useEffect(() => {
    setNodes([]);
    setSelected(null);
    setContent(null);
    if (baseId) void refresh(baseId);
  }, [baseId, refresh]);

  useEffect(() => {
    const timers = ingestTimers.current;
    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer);
      if (lastNodeId.current) {
        const nodeId = lastNodeId.current;
        window.setTimeout(() => void knowledgeNodesApi.ingest(nodeId).catch(() => undefined), 800);
      }
    };
  }, []);

  const scheduleIngest = useCallback((nodeId: string) => {
    const existing = ingestTimers.current.get(nodeId);
    if (existing) window.clearTimeout(existing);
    const timer = window.setTimeout(() => {
      ingestTimers.current.delete(nodeId);
      void knowledgeNodesApi.ingest(nodeId).catch(() => undefined);
    }, INGEST_IDLE_MS);
    ingestTimers.current.set(nodeId, timer);
  }, []);

  const childrenOf = useCallback(
    (node: KnowledgeNode) => nodes.filter((entry) => entry.parent_id === node.id),
    [nodes],
  );

  const openNode = useCallback(
    async (node: KnowledgeNode) => {
      if (lastNodeId.current && lastNodeId.current !== node.id) {
        const previous = lastNodeId.current;
        const timer = ingestTimers.current.get(previous);
        if (timer) window.clearTimeout(timer);
        ingestTimers.current.delete(previous);
        const previousNode = nodes.find((entry) => entry.id === previous);
        if (previousNode?.kind !== "folder") {
          window.setTimeout(
            () => void knowledgeNodesApi.ingest(previous).catch(() => undefined),
            800,
          );
        }
      }
      lastNodeId.current = node.kind === "folder" ? "" : node.id;
      setSelected(node);
      setContent(null);
      setCsvView({});
      setMobileOpen(false);
      if (node.kind === "folder") {
        return;
      }
      try {
        const payload = await knowledgeNodesApi.content(node.id);
        setContent(payload);
        if (payload.base_mtime !== undefined) mtimeRef.current.set(node.id, payload.base_mtime);
        if (node.kind === "csv") {
          try {
            const view = await knowledgeNodesApi.viewState(node.id);
            setCsvView((view.state ?? {}) as CsvViewState);
          } catch {
            setCsvView({});
          }
        }
      } catch (cause) {
        setError(describeError(cause, t("knowledge.loadFailed")));
      }
    },
    [nodes, t],
  );

  const submitCreate = useCallback(async () => {
    if (!creating || !creating.name.trim()) return;
    try {
      if (creating.kind === "link") {
        await knowledgeNodesApi.create({
          baseId,
          parentId: creating.parentId,
          kind: "link",
          name: creating.name.trim(),
          url: creating.url.trim(),
        });
      } else {
        await knowledgeNodesApi.create({
          baseId,
          parentId: creating.parentId,
          kind: creating.kind,
          name:
            creating.kind === "md"
              ? withExtension(creating.name, "md")
              : creating.kind === "csv"
                ? withExtension(creating.name, "csv")
                : creating.name.trim(),
        });
      }
      setCreating(null);
      await refresh(baseId);
    } catch (cause) {
      setError(describeError(cause, t("knowledge.saveFailed")));
    }
  }, [baseId, creating, refresh, t]);

  const submitRename = useCallback(async () => {
    if (!renaming) return;
    try {
      const updated = await knowledgeNodesApi.patch(renaming.id, { name: renaming.value.trim() });
      setRenaming(null);
      await refresh(baseId);
      setSelected((current) => (current?.id === updated.id ? updated : current));
    } catch (cause) {
      setError(describeError(cause, t("knowledge.saveFailed")));
    }
  }, [baseId, refresh, renaming, t]);

  const removeNode = useCallback(async () => {
    if (!deleting) return;
    try {
      await knowledgeNodesApi.remove(deleting.id);
      const parentId = deleting.parent_id;
      if (selected && (selected.id === deleting.id || isInside(nodes, selected.id, deleting.id))) {
        const parent = nodes.find((node) => node.id === parentId) ?? null;
        setSelected(parent);
        setContent(null);
      }
      setDeleting(null);
      await refresh(baseId);
    } catch (cause) {
      setError(describeError(cause, t("knowledge.saveFailed")));
    }
  }, [baseId, deleting, nodes, refresh, selected, t]);

  const triggerUpload = useCallback((parentId: string) => {
    uploadTarget.current = parentId;
    fileInput.current?.click();
  }, []);

  const onFilePicked = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      event.target.value = "";
      if (!file) return;
      const request = knowledgeNodesApi.uploadWithProgress({
        baseId,
        parentId: uploadTarget.current,
        file,
        onProgress: (ratio) =>
          setUploading((current) => (current ? { ...current, ratio } : current)),
      });
      setUploading({ name: file.name, ratio: 0, abort: request.abort });
      request.promise
        .then(async () => {
          await refresh(baseId);
        })
        .catch((cause) => {
          if (cause instanceof ApiError && cause.message === "上传已取消") return;
          setError(describeError(cause, t("knowledge.uploadFailed")));
        })
        .finally(() => setUploading(null));
    },
    [baseId, refresh, t],
  );

  const openMove = useCallback(
    async (node: KnowledgeNode) => {
      const folders = nodes.filter(
        (entry) =>
          entry.kind === "folder" && entry.id !== node.id && !isInside(nodes, entry.id, node.id),
      );
      setMoving({ node, folders });
    },
    [nodes],
  );

  const applyMove = useCallback(
    async (parentId: string) => {
      if (!moving) return;
      try {
        const updated = await knowledgeNodesApi.patch(moving.node.id, { parentId });
        setMoving(null);
        await refresh(baseId);
        setSelected((current) => (current?.id === updated.id ? updated : current));
      } catch (cause) {
        setError(describeError(cause, t("knowledge.saveFailed")));
      }
    },
    [baseId, moving, refresh, t],
  );

  const openAssign = useCallback(async (node: KnowledgeNode) => {
    try {
      const payload = await companyApi.employees();
      const employees = (payload.employees ?? []).filter(
        (employee) => employee.status !== "retired",
      );
      setAssigning({ node, employees });
    } catch (cause) {
      setError(describeError(cause, t("knowledge.loadFailed")));
    }
  }, [t]);

  const applyAssign = useCallback(
    async (employeeId: string | null) => {
      if (!assigning) return;
      try {
        const updated = await knowledgeNodesApi.patch(assigning.node.id, { employeeId });
        setAssigning(null);
        await refresh(baseId);
        setSelected((current) => (current?.id === updated.id ? updated : current));
      } catch (cause) {
        setError(describeError(cause, t("knowledge.saveFailed")));
      }
    },
    [assigning, baseId, refresh, t],
  );

  const loadConflictVersion = useCallback((nodeId: string) => {
    const payload = conflictVersions.current.get(nodeId);
    if (!payload || selectedRef.current?.id !== nodeId) return;
    mtimeRef.current.set(nodeId, payload.base_mtime ?? Date.now());
    setContent(payload);
    setEditorVersion((value) => value + 1);
    toast.success(t("knowledge.diskLoaded"));
  }, [t]);

  const saveMarkdown = useCallback(
    async (nodeId: string, text: string) => {
      const baseMtime = mtimeRef.current.get(nodeId);
      try {
        const result = await knowledgeNodesApi.saveText(nodeId, text, baseMtime, false);
        mtimeRef.current.set(nodeId, result.saved_at);
        scheduleIngest(nodeId);
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 409) throw cause;
        const disk = await knowledgeNodesApi.content(nodeId).catch(() => null);
        const result = await knowledgeNodesApi.saveText(nodeId, text, baseMtime, false, true);
        mtimeRef.current.set(nodeId, result.saved_at);
        scheduleIngest(nodeId);
        if (disk?.text !== undefined) {
          conflictVersions.current.set(nodeId, disk);
          toast.warning(t("knowledge.saveConflictOverwritten"), {
            action: {
              label: t("knowledge.loadDiskVersion"),
              onClick: () => loadConflictVersion(nodeId),
            },
          });
        }
      }
    },
    [loadConflictVersion, scheduleIngest, t],
  );

  const captureSnapshot = useCallback(async () => {
    if (!selected || selected.kind !== "link") return;
    setSnapshotting(true);
    try {
      await knowledgeNodesApi.snapshot(selected.id);
      const payload = await knowledgeNodesApi.content(selected.id);
      setContent(payload);
      toast.success(t("knowledge.snapshotReady"));
    } catch (cause) {
      setError(describeError(cause, t("knowledge.snapshotFailed")));
    } finally {
      setSnapshotting(false);
    }
  }, [selected, t]);

  const persistCsvView = useCallback(
    (state: CsvViewState) => {
      if (!selected || selected.kind !== "csv") return;
      void knowledgeNodesApi
        .saveViewState(selected.id, state as unknown as Record<string, unknown>)
        .catch((cause) => setError(describeError(cause, t("knowledge.saveFailed"))));
    },
    [selected, t],
  );

  const selectedFolder = useMemo(
    () => (selected?.kind === "folder" ? selected : null),
    [selected],
  );
  const deleteCount = useMemo(
    () => (deleting ? 1 + nodes.filter((node) => isInside(nodes, node.id, deleting.id)).length : 0),
    [deleting, nodes],
  );

  const tree = (
    <NodeTree
      key={baseId}
      nodes={nodes}
      onAssign={(node) => void openAssign(node)}
      onCreate={(parentId, kind) => {
        const target = parentId || selectedFolder?.id || "";
        setCreating({ parentId: target, kind, name: "", url: "" });
      }}
      onDelete={(node) => setDeleting(node)}
      onMove={(node) => void openMove(node)}
      onOpen={(node) => void openNode(node)}
      onRenameCancel={() => setRenaming(null)}
      onRenameChange={(value) =>
        setRenaming((current) => (current ? { ...current, value } : current))
      }
      onRenameStart={(node) => setRenaming({ id: node.id, value: node.name })}
      onRenameSubmit={() => void submitRename()}
      onUpload={(parentId) => triggerUpload(parentId || selectedFolder?.id || "")}
      onWidthChange={setWidth}
      query={debounced}
      renaming={renaming}
      selectedId={selected?.id ?? null}
      width={width}
    />
  );

  return (
    <div className="border-chat-hairline bg-card flex h-full min-h-0 overflow-hidden rounded-xl border">
      {!mobile ? tree : null}
      <section className="flex min-w-0 flex-1 flex-col">
        {mobile ? (
          <div className="border-b border-[var(--divider)] px-4 py-1.5">
            <Button
              aria-label={t("knowledge.treeTitle")}
              onClick={() => setMobileOpen(true)}
              size="icon-sm"
              variant="ghost"
            >
              <Menu className="size-4" />
            </Button>
          </div>
        ) : null}
        {uploading ? (
          <div className="border-b border-[var(--divider)] px-4 py-1.5">
            <div className="flex items-center gap-2">
              <span className="text-chat-muted min-w-0 flex-1 truncate text-[11px]">
                {t("knowledge.uploading")}：{uploading.name}
              </span>
              <Progress className="h-1 w-32" value={uploading.ratio * 100} />
              <button
                className="text-chat-muted hover:text-chat-ink text-[11px] underline"
                onClick={() => uploading.abort()}
                type="button"
              >
                {t("knowledge.cancelUpload")}
              </button>
            </div>
          </div>
        ) : null}
        {error ? (
          <div className="border-b border-[var(--divider)] px-4 py-1.5">
            <p className="text-destructive text-xs">
              {error}
              <button
                className="ml-2 underline"
                onClick={() => setError("")}
                type="button"
              >
                {t("knowledge.dismiss")}
              </button>
            </p>
          </div>
        ) : null}

        <div className="min-h-0 flex-1 overflow-auto">
          {selected ? (
            <>
              <header className="border-chat-hairline flex items-center gap-2 border-b px-4 py-2.5">
                <span className="text-chat-ink min-w-0 flex-1 truncate text-sm font-medium">
                  {selected.name}
                </span>
                {selected.employee_id ? (
                  <span className="bg-primary/5 text-chat-muted rounded-full px-2 py-0.5 text-[11px]">
                    {t("knowledge.employeeOnly")}
                  </span>
                ) : null}
                {selected.kind !== "folder" ? (
                  <Button
                    aria-label={t("knowledge.download")}
                    onClick={() =>
                      window.open(knowledgeNodesApi.downloadUrl(selected.id), "_blank")
                    }
                    size="icon-sm"
                    variant="outline"
                  >
                    <Download className="size-3.5" />
                  </Button>
                ) : null}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button aria-label={t("knowledge.more")} size="icon-sm" variant="ghost">
                      <MoreHorizontal className="size-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-36">
                    <DropdownMenuItem onSelect={() => setRenaming({ id: selected.id, value: selected.name })}>
                      {t("knowledge.rename")}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onSelect={() =>
                        window.open(knowledgeNodesApi.downloadUrl(selected.id), "_blank")
                      }
                    >
                      {t("knowledge.download")}
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem variant="destructive" onSelect={() => setDeleting(selected)}>
                      {t("knowledge.delete")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </header>
              <div className="min-h-0">
                {selected.kind === "md" && content?.text !== undefined ? (
                  <MarkdownEditor
                    initialText={content.text}
                    key={`${selected.id}:${editorVersion}`}
                    onSave={(text) => saveMarkdown(selected.id, text)}
                    onUploadAsset={(file) => knowledgeNodesApi.uploadAsset(selected.id, file)}
                  />
                ) : selected.kind === "csv" && content?.text !== undefined ? (
                  <CsvTable
                    initialState={csvView}
                    key={selected.id}
                    onPersist={persistCsvView}
                    text={content.text}
                  />
                ) : selected.kind === "xlsx" ? (
                  <XlsxView key={selected.id} nodeId={selected.id} />
                ) : selected.kind === "pptx" ? (
                  <PptxView key={selected.id} nodeId={selected.id} />
                ) : (
                  <NodeBody
                    content={content}
                    items={childrenOf(selected)}
                    node={selected}
                    onOpenChild={(node) => void openNode(node)}
                    onSnapshot={() => void captureSnapshot()}
                    snapshotting={snapshotting}
                  />
                )}
              </div>
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center py-16">
              <p className="text-chat-muted text-sm">{t("knowledge.emptyDetail")}</p>
            </div>
          )}
        </div>
      </section>

      <input
        accept=".md,.markdown,.txt,.csv,.docx,.doc,.xlsx,.xls,.pptx,.ppt,.pdf,.html,.htm"
        className="hidden"
        onChange={onFilePicked}
        ref={fileInput}
        type="file"
      />

      <Sheet onOpenChange={setMobileOpen} open={mobileOpen}>
        <SheetContent className="flex w-80 flex-col p-0" side="left">
          <SheetHeader>
            <SheetTitle>{t("knowledge.treeTitle")}</SheetTitle>
          </SheetHeader>
          <div className="flex min-h-0 flex-1">{tree}</div>
        </SheetContent>
      </Sheet>

      <Dialog
        onOpenChange={(open) => (!open ? setCreating(null) : undefined)}
        open={Boolean(creating)}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{createTitle(creating?.kind, t)}</DialogTitle>
            <DialogDescription>{t("knowledge.createHint")}</DialogDescription>
          </DialogHeader>
          {creating ? (
            <div className="flex flex-col gap-2">
              <Input
                autoFocus
                onChange={(event) => setCreating({ ...creating, name: event.target.value })}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void submitCreate();
                }}
                placeholder={
                  creating.kind === "link" ? t("knowledge.linkName") : t("knowledge.namePlaceholder")
                }
                value={creating.name}
              />
              {creating.kind === "link" ? (
                <Input
                  onChange={(event) => setCreating({ ...creating, url: event.target.value })}
                  placeholder={t("knowledge.linkUrl")}
                  value={creating.url}
                />
              ) : null}
            </div>
          ) : null}
          <DialogFooter>
            <Button onClick={() => setCreating(null)} variant="outline">
              {t("knowledge.cancel")}
            </Button>
            <Button onClick={() => void submitCreate()}>{t("knowledge.create")}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog onOpenChange={(open) => (!open ? setMoving(null) : undefined)} open={Boolean(moving)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t("knowledge.moveTo")}</DialogTitle>
            <DialogDescription>{moving?.node.name ?? ""}</DialogDescription>
          </DialogHeader>
          <div className="max-h-72 overflow-y-auto">
            <button
              className="hover:bg-primary/5 flex h-9 w-full items-center rounded-lg px-2 text-left text-sm"
              onClick={() => void applyMove("")}
              type="button"
            >
              {t("knowledge.rootFolder")}
            </button>
            {(moving?.folders ?? []).map((folder) => (
              <button
                className="hover:bg-primary/5 flex h-9 w-full items-center rounded-lg px-2 text-left text-sm"
                key={folder.id}
                onClick={() => void applyMove(folder.id)}
                type="button"
              >
                {folder.name}
              </button>
            ))}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        onOpenChange={(open) => (!open ? setAssigning(null) : undefined)}
        open={Boolean(assigning)}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t("knowledge.assignEmployee")}</DialogTitle>
            <DialogDescription>{assigning?.node.name ?? ""}</DialogDescription>
          </DialogHeader>
          <div className="max-h-72 overflow-y-auto">
            <button
              className="hover:bg-primary/5 flex h-9 w-full items-center rounded-lg px-2 text-left text-sm"
              onClick={() => void applyAssign(null)}
              type="button"
            >
              {t("knowledge.shared")}
            </button>
            {(assigning?.employees ?? []).map((employee) => (
              <button
                className="hover:bg-primary/5 flex h-9 w-full items-center rounded-lg px-2 text-left text-sm"
                key={employee.employee_id}
                onClick={() => void applyAssign(employee.employee_id)}
                type="button"
              >
                {employee.name}
              </button>
            ))}
            {assigning && assigning.employees.length === 0 ? (
              <p className="text-chat-muted px-2 py-4 text-xs">{t("knowledge.noEmployees")}</p>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog
        onOpenChange={(open) => (!open ? setDeleting(null) : undefined)}
        open={Boolean(deleting)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("knowledge.delete")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("knowledge.deleteConfirm").replace("{count}", String(deleteCount))}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("knowledge.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => void removeNode()}>
              {t("knowledge.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function withExtension(name: string, extension: string): string {
  const trimmed = name.trim();
  return trimmed.toLowerCase().endsWith(`.${extension}`) ? trimmed : `${trimmed}.${extension}`;
}

function isInside(nodes: KnowledgeNode[], candidateId: string, ancestorId: string): boolean {
  const parentOf = new Map(nodes.map((node) => [node.id, node.parent_id]));
  let cursor = parentOf.get(candidateId) ?? "";
  while (cursor) {
    if (cursor === ancestorId) return true;
    cursor = parentOf.get(cursor) ?? "";
  }
  return false;
}

function createTitle(kind: CreateKind | undefined, t: (key: string) => string): string {
  if (kind === "folder") return t("knowledge.newFolder");
  if (kind === "csv") return t("knowledge.newCsv");
  if (kind === "link") return t("knowledge.addLink");
  return t("knowledge.newMarkdown");
}
