/**
  * 功能：右栏文件视图（v5 /，@base docs/前端设计规划.md）——两类作用的文件树
 * 会话 = 会话文件根（project_root 优先）；员工单聊 = 项目文件树（项目端点）。
 * 懒加载逐级展开（点击目录才拉取子级）；忽略名单由后端过滤；点击文件开预览页签
 * （员工态页签带 projectId → 预览走项目端点）。
 * 定位：fileFocusPath（制品引用/文件打开）→ 自动逐级展开到父目录并高亮目标行。
 */
import { ChevronRight, FileText, Folder, FolderOpen, Loader2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useT } from "@/i18n";
import { formatSize } from "@/lib/attachments";
import type { PanelScope } from "@/lib/panel-scope";
import { cn } from "@/lib/utils";
import { panelApi, type FileTreeEntry } from "@/services/panel";
import { usePanelStore } from "@/stores/panel-store";

/** 功能：目录行缩进步长（px）。 */
const INDENT_STEP = 12;

export function FilesView({ scope }: { scope: PanelScope }) {
  // 按作用域分流：员工单聊 = 项目文件树（接入）；会话 = 会话文件树
  if (scope.kind === "employee") return <ProjectFilesView projectId={scope.projectId} />;
  return <SessionFilesView sessionId={scope.sessionId} />;
}

function SessionFilesView({ sessionId }: { sessionId: string }) {
  const focusFile = usePanelStore((state) => state.focusFile);
  const loadEntries = useCallback(
    (path: string) => panelApi.files(sessionId, path).then((payload) => payload.entries),
    [sessionId],
  );
  return (
    <FileTree
      rootKey={sessionId}
      loadEntries={loadEntries}
      onOpenFile={(path, name) => focusFile(path, name)}
    />
  );
}

function ProjectFilesView({ projectId }: { projectId: string }) {
  const focusFile = usePanelStore((state) => state.focusFile);
  const loadEntries = useCallback(
    (path: string) => panelApi.projectFiles(projectId, path).then((payload) => payload.entries),
    [projectId],
  );
  return (
    <FileTree
      rootKey={projectId}
      loadEntries={loadEntries}
      onOpenFile={(path, name) => focusFile(path, name, projectId)}
    />
  );
}

/** 功能：文件树（会话/项目共享渲染）——rootKey 切换重置、loadEntries 数据源、onOpenFile 开页签。 */
function FileTree({
  rootKey,
  loadEntries,
  onOpenFile,
}: {
  rootKey: string;
  loadEntries: (path: string) => Promise<FileTreeEntry[]>;
  onOpenFile: (path: string, name: string) => void;
}) {
  const t = useT();
  const focusPath = usePanelStore((state) => state.fileFocusPath);
  const tabs = usePanelStore((state) => state.tabs);
  const activeTabId = usePanelStore((state) => state.activeTabId);
  const [entriesByPath, setEntriesByPath] = useState<Record<string, FileTreeEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [failed, setFailed] = useState<Set<string>>(new Set());
  /** 已发起加载的 path（防重复请求 + 让定位 effect 不依赖 entries 状态）。 */
  const loadedRef = useRef<Set<string>>(new Set());
  const rootLoadedRef = useRef("");

  const activePath = useMemo(() => {
    const tab = tabs.find((item) => item.id === activeTabId);
    return tab?.kind === "file" ? (tab.path ?? "") : "";
  }, [tabs, activeTabId]);

  const load = useCallback(
    (path: string) => {
      if (loadedRef.current.has(path)) return;
      loadedRef.current.add(path);
      loadEntries(path)
        .then((entries) => {
          setEntriesByPath((prev) => ({ ...prev, [path]: entries }));
        })
        .catch(() => {
          setFailed((prev) => new Set(prev).add(path));
        });
    },
    [loadEntries],
  );

  // 切作用域：重置树并加载根级
  useEffect(() => {
    setEntriesByPath({});
    setExpanded(new Set());
    setFailed(new Set());
    loadedRef.current = new Set();
    rootLoadedRef.current = "";
    if (!rootKey) return;
    rootLoadedRef.current = rootKey;
    load("");
  }, [rootKey, load]);

  // /文件打开定位：逐级展开 focusPath 的祖先目录
  useEffect(() => {
    if (!focusPath || rootLoadedRef.current !== rootKey) return;
    const parts = focusPath.split("/").filter(Boolean);
    const dirs = parts.slice(0, -1);
    let acc = "";
    const chain: string[] = [];
    for (const dir of dirs) {
      acc = acc ? `${acc}/${dir}` : dir;
      chain.push(acc);
    }
    if (chain.length) {
      setExpanded((prev) => {
        const next = new Set(prev);
        for (const dir of chain) next.add(dir);
        return next;
      });
      for (const dir of chain) load(dir);
    }
  }, [focusPath, rootKey, load]);

  const toggleDir = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
    load(path);
  };

  const renderLevel = (path: string, depth: number): ReactNode[] => {
    const list = entriesByPath[path] ?? [];
    return list.flatMap((entry) => {
      const childPath = path ? `${path}/${entry.name}` : entry.name;
      const pad = depth * INDENT_STEP + 8;
      if (entry.type === "dir") {
        const isOpen = expanded.has(childPath);
        const children = entriesByPath[childPath];
        return [
          <button
            key={`d:${childPath}`}
            type="button"
            data-testid={`panel-dir-${childPath}`}
            className="text-chat-ink hover:bg-primary/5 flex w-full items-center gap-1 rounded-md py-1 pr-2 text-left text-xs"
            style={{ paddingLeft: pad }}
            onClick={() => toggleDir(childPath)}
          >
            <ChevronRight
              className={cn(
                "text-chat-muted size-3 shrink-0 transition-transform",
                isOpen && "rotate-90",
              )}
            />
            {isOpen ? (
              <FolderOpen className="text-chat-muted size-3.5 shrink-0" />
            ) : (
              <Folder className="text-chat-muted size-3.5 shrink-0" />
            )}
            <span className="min-w-0 flex-1 truncate">{entry.name}</span>
          </button>,
          ...(isOpen
            ? children
              ? renderLevel(childPath, depth + 1)
              : [
                  <div
                    key={`l:${childPath}`}
                    className="text-chat-muted flex items-center gap-1 py-1 text-[11px]"
                    style={{ paddingLeft: pad + INDENT_STEP }}
                    data-testid={`panel-dir-loading-${childPath}`}
                  >
                    <Loader2 className="size-3 animate-spin" />
                    {failed.has(childPath) ? t("chat.panel.fileLoadFailed") : null}
                  </div>,
                ]
            : []),
        ];
      }
      const isActive = activePath === childPath || focusPath === childPath;
      return [
        <button
          key={`f:${childPath}`}
          type="button"
          data-testid={`panel-file-${childPath}`}
          className={cn(
            "flex w-full items-center gap-1 rounded-md py-1 pr-2 text-left text-xs",
            isActive ? "bg-primary/10 text-primary" : "text-chat-ink hover:bg-primary/5",
          )}
          style={{ paddingLeft: pad + INDENT_STEP }}
          onClick={() => onOpenFile(childPath, entry.name)}
        >
          <FileText className="text-chat-muted size-3.5 shrink-0" />
          <span className="min-w-0 flex-1 truncate">{entry.name}</span>
          <span className="text-chat-muted shrink-0 text-[11px]">{formatSize(entry.size)}</span>
        </button>,
      ];
    });
  };

  const rootEntries = entriesByPath[""];
  return (
    <div className="px-1 py-1" data-testid="panel-files">
      {rootEntries === undefined && !failed.has("") ? (
        <p className="text-chat-muted px-2 py-2 text-xs">{t("chat.panel.previewLoading")}</p>
      ) : failed.has("") ? (
        <p className="text-destructive px-2 py-2 text-xs">{t("chat.panel.fileLoadFailed")}</p>
      ) : rootEntries.length === 0 ? (
        <p className="text-chat-muted px-2 py-2 text-xs">{t("chat.panel.filesEmpty")}</p>
      ) : (
        renderLevel("", 0)
      )}
    </div>
  );
}
