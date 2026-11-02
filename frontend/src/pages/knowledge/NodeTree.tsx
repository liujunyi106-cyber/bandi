/**
 * 功能：资料库左侧树——搜索/展开/键盘导航/虚拟滚动/宽度拖拽/行内改名与行菜单。
 * 视觉：行悬浮/选中 `bg-primary/5`，图标钮 `size-6 rounded-full`，菜单项 `focus:bg-primary/5`。
 */
import {
  ChevronRight,
  File,
  FileCode2,
  FileSpreadsheet,
  FileText,
  FileType2,
  Folder,
  Link2,
  MoreHorizontal,
  Presentation,
  Table2,
} from "lucide-react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import type { KnowledgeNode } from "@/services/knowledge-nodes";

const ROW_HEIGHT = 32;
const VIRTUAL_THRESHOLD = 300;

const KIND_ICON: Record<string, typeof File> = {
  folder: Folder,
  md: FileText,
  csv: Table2,
  docx: FileType2,
  xlsx: FileSpreadsheet,
  pptx: Presentation,
  pdf: File,
  html: FileCode2,
  link: Link2,
};

export interface NodeTreeProps {
  nodes: KnowledgeNode[];
  width: number;
  query: string;
  selectedId: string | null;
  renaming: { id: string; value: string } | null;
  onWidthChange: (width: number) => void;
  onOpen: (node: KnowledgeNode) => void;
  onRenameStart: (node: KnowledgeNode) => void;
  onRenameChange: (value: string) => void;
  onRenameSubmit: () => void;
  onRenameCancel: () => void;
  onMove: (node: KnowledgeNode) => void;
  onAssign: (node: KnowledgeNode) => void;
  onDelete: (node: KnowledgeNode) => void;
  onCreate: (parentId: string, kind: "folder" | "md" | "csv" | "link") => void;
  onUpload: (parentId: string) => void;}

interface Row {
  node: KnowledgeNode;
  depth: number;
}

export function NodeTree(props: NodeTreeProps) {
  const t = useT();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [activeId, setActiveId] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const byParent = useMemo(() => {
    const map = new Map<string, KnowledgeNode[]>();
    for (const node of props.nodes) {
      const bucket = map.get(node.parent_id) ?? [];
      bucket.push(node);
      map.set(node.parent_id, bucket);
    }
    for (const bucket of map.values()) {
      bucket.sort((left, right) => {
        if (left.kind === "folder" && right.kind !== "folder") return -1;
        if (right.kind === "folder" && left.kind !== "folder") return 1;
        return left.sort_order - right.sort_order || left.name.localeCompare(right.name);
      });
    }
    return map;
  }, [props.nodes]);

  const query = props.query.trim().toLowerCase();

  const rows = useMemo(() => {
    const matched = new Set<string>();
    if (query) {
      for (const node of props.nodes) {
        if (node.name.toLowerCase().includes(query)) matched.add(node.id);
      }
    }
    const keep = new Set<string>(matched);
    if (query) {
      const parentOf = new Map(props.nodes.map((node) => [node.id, node.parent_id]));
      for (const id of matched) {
        let cursor = parentOf.get(id) ?? "";
        while (cursor) {
          keep.add(cursor);
          cursor = parentOf.get(cursor) ?? "";
        }
      }
    }
    const output: Row[] = [];
    const walk = (parentId: string, depth: number) => {
      for (const node of byParent.get(parentId) ?? []) {
        if (query && !keep.has(node.id)) continue;
        output.push({ node, depth });
        const open = query ? true : expanded.has(node.id);
        if (node.kind === "folder" && open) walk(node.id, depth + 1);
      }
    };
    walk("", 0);
    return output;
  }, [byParent, expanded, props.nodes, query]);

  const rowIndex = useMemo(() => {
    const map = new Map<string, number>();
    rows.forEach((row, index) => map.set(row.node.id, index));
    return map;
  }, [rows]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    enabled: rows.length > VIRTUAL_THRESHOLD,
  });

  useEffect(() => {
    if (!props.selectedId) return;
    const index = rowIndex.get(props.selectedId);
    if (index === undefined) return;
    setActiveId(props.selectedId);
    virtualizer.scrollToIndex(index, { align: "auto" });
  }, [props.selectedId, rowIndex, virtualizer]);

  const toggle = useCallback((id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (rows.length === 0) return;
      const current = rowIndex.get(activeId);
      const index = current === undefined ? -1 : current;
      const move = (target: number) => {
        const next = Math.max(0, Math.min(rows.length - 1, target));
        setActiveId(rows[next].node.id);
        virtualizer.scrollToIndex(next, { align: "auto" });
      };
      const active = index >= 0 ? rows[index].node : null;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        move(index + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        move(index - 1);
      } else if (event.key === "ArrowRight" && active) {
        event.preventDefault();
        if (active.kind === "folder" && !expanded.has(active.id)) {
          toggle(active.id);
        } else {
          const child = rows[index + 1]?.node;
          if (child && child.parent_id === active.id) setActiveId(child.id);
        }
      } else if (event.key === "ArrowLeft" && active) {
        event.preventDefault();
        if (active.kind === "folder" && expanded.has(active.id)) {
          toggle(active.id);
        } else if (active.parent_id) {
          setActiveId(active.parent_id);
        }
      } else if (event.key === "Enter" && active) {
        event.preventDefault();
        props.onOpen(active);
        if (active.kind === "folder") toggle(active.id);
      } else if (event.key === "F2" && active) {
        event.preventDefault();
        props.onRenameStart(active);
      } else if (event.key === "Delete" && active) {
        event.preventDefault();
        props.onDelete(active);
      }
    },
    [activeId, expanded, props, rowIndex, rows, toggle, virtualizer],
  );

  const startResize = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = props.width;
      const onMove = (moveEvent: PointerEvent) => {
        const next = Math.max(240, Math.min(480, startWidth + moveEvent.clientX - startX));
        props.onWidthChange(next);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [props],
  );

  const highlight = (name: string) => {
    if (!query) return name;
    const index = name.toLowerCase().indexOf(query);
    if (index < 0) return name;
    return (
      <>
        {name.slice(0, index)}
        <mark className="bg-primary/10 text-chat-ink rounded px-0.5">
          {name.slice(index, index + query.length)}
        </mark>
        {name.slice(index + query.length)}
      </>
    );
  };

  const renderRow = (row: Row) => {
    const node = row.node;
    const Icon = KIND_ICON[node.kind] ?? File;
    const selected = props.selectedId === node.id;
    const active = activeId === node.id;
    const open = query ? true : expanded.has(node.id);
    return (
      <div
        aria-level={row.depth + 1}
        aria-selected={selected}
        className={`group flex h-8 cursor-pointer items-center gap-1.5 rounded-lg px-2 text-sm ${
          selected
            ? "bg-primary/5 text-chat-ink"
            : active
              ? "bg-primary/5 text-chat-ink"
              : "text-chat-muted hover:bg-primary/5 hover:text-chat-ink"
        }`}
        data-node-id={node.id}
        key={node.id}
        onClick={() => {
          setActiveId(node.id);
          props.onOpen(node);
          if (node.kind === "folder") toggle(node.id);
        }}
        onDoubleClick={() => props.onRenameStart(node)}
        role="treeitem"
        style={{ paddingLeft: 8 + row.depth * 16 }}
      >
        {node.kind === "folder" ? (
          <ChevronRight
            className={`size-3.5 shrink-0 transition-transform ${open ? "rotate-90" : ""}`}
          />
        ) : (
          <span className="w-3.5 shrink-0" />
        )}
        <Icon aria-hidden="true" className="size-4 shrink-0" />
        {props.renaming?.id === node.id ? (
          <Input
            aria-label={t("knowledge.rename")}
            autoFocus
            className="h-6 px-1 text-xs"
            onBlur={() => props.onRenameSubmit()}
            onChange={(event) => props.onRenameChange(event.target.value)}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              event.stopPropagation();
              if (event.key === "Enter") props.onRenameSubmit();
              if (event.key === "Escape") props.onRenameCancel();
            }}
            value={props.renaming.value}
          />
        ) : (
          <span className="min-w-0 flex-1 truncate">
            {highlight(node.name)}
          </span>
        )}
        {node.employee_id ? (
          <span className="bg-primary/5 text-chat-muted rounded-full px-1.5 text-[10px]">
            {t("knowledge.employeeOnly")}
          </span>
        ) : null}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              aria-label={t("knowledge.more")}
              className="text-chat-muted hover:bg-primary/5 invisible size-6 items-center justify-center rounded-full group-hover:visible aria-expanded:visible"
              onClick={(event) => event.stopPropagation()}
              type="button"
            >
              <MoreHorizontal className="size-3.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-36">
            <DropdownMenuItem onSelect={() => props.onRenameStart(node)}>
              {t("knowledge.rename")}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => props.onMove(node)}>
              {t("knowledge.moveTo")}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => props.onAssign(node)}>
              {t("knowledge.assignEmployee")}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem variant="destructive" onSelect={() => props.onDelete(node)}>
              {t("knowledge.delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    );
  };

  return (
    <>
      <aside
        aria-label={t("knowledge.treeTitle")}
        className="flex min-h-0 shrink-0 flex-col"
        style={{ width: props.width }}
      >
        <div className="flex items-center justify-between px-3 py-2.5">
          <b className="text-chat-ink text-sm">{t("knowledge.treeTitle")}</b>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                aria-label={t("knowledge.add")}
                className="text-chat-muted hover:bg-primary/5 grid size-6 place-items-center rounded-full"
                type="button"
              >
                <span aria-hidden="true" className="text-base leading-none">
                  +
                </span>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-44">
              <DropdownMenuItem onSelect={() => props.onCreate("", "folder")}>
                {t("knowledge.newFolder")}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => props.onCreate("", "md")}>
                {t("knowledge.newMarkdown")}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => props.onCreate("", "csv")}>
                {t("knowledge.newCsv")}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => props.onCreate("", "link")}>
                {t("knowledge.addLink")}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => props.onUpload("")}>
                {t("knowledge.upload")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div
          className="min-h-0 flex-1 overflow-y-auto px-2 pb-2 outline-none"
          data-testid="knowledge-tree"
          onKeyDown={handleKeyDown}
          ref={scrollRef}
          role="tree"
          tabIndex={0}
        >
          {rows.length === 0 ? (
            <p className="text-chat-muted px-3 py-6 text-center text-xs">
              {query ? t("knowledge.emptySearch") : t("knowledge.emptyTree")}
            </p>
          ) : rows.length > VIRTUAL_THRESHOLD ? (
            <div className="relative" style={{ height: virtualizer.getTotalSize() }}>
              {virtualizer.getVirtualItems().map((item) => {
                const row = rows[item.index];
                return (
                  <div
                    className="absolute top-0 left-0 w-full"
                    key={row.node.id}
                    style={{ height: item.size, transform: `translateY(${item.start}px)` }}
                  >
                    {renderRow(row)}
                  </div>
                );
              })}
            </div>
          ) : (
            rows.map((row) => renderRow(row))
          )}
        </div>
      </aside>
      <div
        aria-hidden="true"
        className="hover:bg-primary/20 w-1 shrink-0 cursor-col-resize bg-transparent"
        data-testid="knowledge-tree-resizer"
        onDoubleClick={() => props.onWidthChange(288)}
        onPointerDown={startResize}
      />
    </>
  );
}
