/**
 * 功能：Markdown 块编辑器——contentEditable 块、悬浮格式面板（13 类 + 缩进/对齐/复制剪切删除）、
 * 空行插入面板（19 项单列 + 搜索）、自动保存（600ms 防抖、失败重试一次、超大文本降频）。
 */
import { Plus, Type } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { DiagramCard } from "@/components/chat/DiagramCard";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/i18n";
import { autosaveDelay } from "@/lib/autosave";
import {
  FORMAT_ITEMS,
  INSERT_ITEMS,
  newBlock,
  parseMarkdown,
  serializeBlocks,
  type Block,
  type BlockType,
} from "@/lib/markdown-blocks";

const SAVE_RETRY_MS = 1000;

/** 功能：读取块内文本——浏览器用 innerText 保留软换行，测试环境回退 textContent。 */
function readBlockText(element: HTMLElement): string {
  return typeof element.innerText === "string" ? element.innerText : (element.textContent ?? "");
}

/** 功能：计算光标在块文本中的偏移；无选区时落在末尾。 */
function caretOffset(element: HTMLElement): number {
  const selection = window.getSelection();
  if (
    !selection ||
    selection.rangeCount === 0 ||
    !selection.anchorNode ||
    !element.contains(selection.anchorNode)
  ) {
    return readBlockText(element).length;
  }
  const range = document.createRange();
  range.selectNodeContents(element);
  range.setEnd(selection.anchorNode, selection.anchorOffset);
  return range.toString().length;
}

export function MarkdownEditor({
  initialText,
  onSave,
  onUploadAsset,
}: {
  initialText: string;
  onSave: (text: string) => Promise<unknown> | void;
  onUploadAsset?: (file: File) => Promise<{ name: string; url: string }>;
}) {
  const t = useT();
  const [blocks, setBlocks] = useState<Block[]>(() => parseMarkdown(initialText));
  const [revision, setRevision] = useState(0);
  const bumpRevision = useCallback(() => setRevision((value) => value + 1), []);
  const [insertFor, setInsertFor] = useState<string | null>(null);
  const [insertQuery, setInsertQuery] = useState("");
  const [formatFor, setFormatFor] = useState<string | null>(null);
  const blocksRef = useRef(blocks);
  const dirtyRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const saveRef = useRef(onSave);
  const refs = useRef<Map<string, HTMLDivElement>>(new Map());
  const dragId = useRef<string | null>(null);
  const tRef = useRef(t);

  blocksRef.current = blocks;
  saveRef.current = onSave;
  tRef.current = t;

  const dropOn = (targetId: string) => (event: React.DragEvent) => {
    event.preventDefault();
    const sourceId = dragId.current;
    dragId.current = null;
    if (!sourceId || sourceId === targetId) return;
    setBlocks((current) => {
      const from = current.findIndex((item) => item.id === sourceId);
      const to = current.findIndex((item) => item.id === targetId);
      if (from < 0 || to < 0) return current;
      const next = [...current];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
    scheduleSave();
  };

  const historyRef = useRef<{ past: Block[][]; future: Block[][]; lastPush: number }>({
    past: [],
    future: [],
    lastPush: 0,
  });

  const flush = useCallback(async (attempt = 0) => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    if (!dirtyRef.current) return;
    dirtyRef.current = false;
    try {
      await saveRef.current(serializeBlocks(blocksRef.current));
    } catch {
      dirtyRef.current = true;
      if (attempt === 0) {
        timerRef.current = window.setTimeout(() => void flush(1), SAVE_RETRY_MS);
        return;
      }
      toast.error(tRef.current("knowledge.autosaveFailed"));
    }
  }, []);

  const scheduleSave = useCallback(() => {
    dirtyRef.current = true;
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    const chars = blocksRef.current.reduce((total, block) => total + block.text.length, 0);
    timerRef.current = window.setTimeout(() => void flush(), autosaveDelay(chars));
  }, [flush]);

  useEffect(
    () => () => {
      void flush();
    },
    [flush],
  );

  const focusBlock = useCallback((id: string) => {
    window.setTimeout(() => {
      const element = refs.current.get(id);
      if (!element) return;
      element.focus();
      const range = document.createRange();
      range.selectNodeContents(element);
      range.collapse(false);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
    }, 0);
  }, []);

  const pushHistory = useCallback((force = false) => {
    const history = historyRef.current;
    const now = Date.now();
    if (!force && now - history.lastPush < 600) return;
    history.past.push(blocksRef.current.map((block) => ({ ...block })));
    if (history.past.length > 100) history.past.shift();
    history.future = [];
    history.lastPush = now;
  }, []);

  const undo = useCallback(() => {
    const history = historyRef.current;
    const previous = history.past.pop();
    if (!previous) return;
    history.future.push(blocksRef.current.map((block) => ({ ...block })));
    setBlocks(previous);
    bumpRevision();
    scheduleSave();
  }, [bumpRevision, scheduleSave]);

  const redo = useCallback(() => {
    const history = historyRef.current;
    const next = history.future.pop();
    if (!next) return;
    history.past.push(blocksRef.current.map((block) => ({ ...block })));
    setBlocks(next);
    bumpRevision();
    scheduleSave();
  }, [bumpRevision, scheduleSave]);

  const updateText = useCallback(
    (id: string, text: string) => {
      pushHistory();
      setBlocks((current) => current.map((block) => (block.id === id ? { ...block, text } : block)));
      scheduleSave();
    },
    [pushHistory, scheduleSave],
  );

  const patchBlock = useCallback(
    (id: string, patch: Partial<Block>) => {
      pushHistory(true);
      setBlocks((current) =>
        current.map((block) => (block.id === id ? { ...block, ...patch } : block)),
      );
      scheduleSave();
    },
    [pushHistory, scheduleSave],
  );

  const changeType = useCallback(
    (id: string, type: BlockType) => {
      patchBlock(id, { type });
      setFormatFor(null);
      focusBlock(id);
    },
    [focusBlock, patchBlock],
  );

  const insertAfter = useCallback(
    (id: string, type: BlockType) => {
      const block = newBlock(type);
      pushHistory(true);
      setBlocks((current) => {
        const index = current.findIndex((item) => item.id === id);
        const next = [...current];
        next.splice(index + 1, 0, block);
        return next;
      });
      setInsertFor(null);
      setInsertQuery("");
      scheduleSave();
      bumpRevision();
      focusBlock(block.id);
    },
    [bumpRevision, focusBlock, pushHistory, scheduleSave],
  );

  const removeBlock = useCallback(
    (id: string) => {
      pushHistory(true);
      setBlocks((current) => {
        if (current.length <= 1) return current;
        const index = current.findIndex((item) => item.id === id);
        const next = current.filter((block) => block.id !== id);
        const previous = next[Math.max(0, index - 1)];
        if (previous) focusBlock(previous.id);
        return next;
      });
      scheduleSave();
      bumpRevision();
    },
    [bumpRevision, focusBlock, pushHistory, scheduleSave],
  );

  const moveBlock = useCallback(
    (id: string, delta: -1 | 1) => {
      pushHistory(true);
      setBlocks((current) => {
        const index = current.findIndex((item) => item.id === id);
        const target = index + delta;
        if (index < 0 || target < 0 || target >= current.length) return current;
        const next = [...current];
        [next[index], next[target]] = [next[target], next[index]];
        return next;
      });
      scheduleSave();
      focusBlock(id);
    },
    [focusBlock, pushHistory, scheduleSave],
  );

  const indent = useCallback(
    (block: Block, delta: 1 | -1) => {
      const lines = (block.text || "").split("\n");
      const next = lines
        .map((line) => (delta > 0 ? `  ${line}` : line.replace(/^ {1,2}/, "")))
        .join("\n");
      updateText(block.id, next);
      bumpRevision();
      focusBlock(block.id);
    },
    [bumpRevision, focusBlock, updateText],
  );

  const copyBlock = useCallback(async (block: Block) => {
    try {
      await navigator.clipboard?.writeText(block.text);
      toast.success(tRef.current("knowledge.copied"));
    } catch {
      toast.error(tRef.current("knowledge.copyFailed"));
    }
  }, []);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>, block: Block) => {
      const mod = event.metaKey || event.ctrlKey;
      if (mod && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) redo();
        else undo();
        return;
      }
      if (mod && event.key.toLowerCase() === "y") {
        event.preventDefault();
        redo();
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        const element = event.currentTarget;
        if (event.shiftKey) {
          const text = readBlockText(element);
          if (text.startsWith("  ")) updateText(block.id, text.slice(2));
        } else {
          document.execCommand("insertText", false, "  ");
          updateText(block.id, readBlockText(element));
        }
        return;
      }
      if (event.altKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
        event.preventDefault();
        moveBlock(block.id, event.key === "ArrowUp" ? -1 : 1);
        return;
      }
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const element = event.currentTarget;
        const text = readBlockText(element);
        const caret = caretOffset(element);
        const before = text.slice(0, caret);
        const after = text.slice(caret);
        if (after === "" && ["ul", "ol", "todo"].includes(block.type)) {
          if (!(before ?? "").trim()) {
            patchBlock(block.id, { type: "text" });
            focusBlock(block.id);
            return;
          }
          pushHistory(true);
          const created = newBlock(block.type);
          setBlocks((current) => {
            const index = current.findIndex((item) => item.id === block.id);
            const next = [...current];
            next[index] = { ...block, text: before };
            next.splice(index + 1, 0, created);
            return next;
          });
          scheduleSave();
          focusBlock(created.id);
          return;
        }
        const created = newBlock("text", after);
        pushHistory(true);
        setBlocks((current) => {
          const index = current.findIndex((item) => item.id === block.id);
          const next = [...current];
          next[index] = { ...block, text: before };
          next.splice(index + 1, 0, created);
          return next;
        });
        scheduleSave();
        focusBlock(created.id);
        return;
      }
      if (event.key === "Backspace" && readBlockText(event.currentTarget) === "") {
        event.preventDefault();
        removeBlock(block.id);
      }
    },
    [focusBlock, moveBlock, patchBlock, pushHistory, redo, removeBlock, scheduleSave, undo, updateText],
  );

  const filteredInsert = useMemo(() => {
    const query = insertQuery.trim().toLowerCase();
    if (!query) return INSERT_ITEMS;
    return INSERT_ITEMS.filter((item) => t(item.labelKey).toLowerCase().includes(query));
  }, [insertQuery, t]);

  const renderBlock = (block: Block) => {
    if (["image", "video", "file"].includes(block.type) && onUploadAsset) {
      return (
        <MediaBlock
          block={block}
          onText={(text) => updateText(block.id, text)}
          onUpload={onUploadAsset}
        />
      );
    }
    if (block.type === "table") {
      return <TableBlock block={block} onText={(text) => updateText(block.id, text)} />;
    }
    if (block.type === "code") {
      return (
        <CodeBlock
          block={block}
          onChange={(text) => updateText(block.id, text)}
          onChangeLanguage={(language) => patchBlock(block.id, { language })}
        />
      );
    }
    if (block.type === "diagram") {
      return <DiagramBlock block={block} onChange={(text) => updateText(block.id, text)} />;
    }
    if (block.type === "columns") {
      return <ColumnsBlock block={block} onChange={(text) => updateText(block.id, text)} />;
    }
    return (
      <EditableBlock
        block={block}
        className={blockClassName(block.type)}
        key={`${block.id}:${revision}`}
        onBlur={() => void flush()}
        onInput={(text) => updateText(block.id, text)}
        onKeyDown={(event) => handleKeyDown(event, block)}
        register={(element) => {
          if (element) refs.current.set(block.id, element);
          else refs.current.delete(block.id);
        }}
      />
    );
  };

  return (
    <div className="mx-auto w-full max-w-[860px] px-6 py-8">
      {blocks.map((block) => (
        <div
          className="group relative"
          key={block.id}
          onDragOver={(event) => event.preventDefault()}
          onDrop={dropOn(block.id)}
        >
          <button
            aria-label={t("knowledge.dragBlock")}
            className="text-chat-muted hover:bg-primary/5 absolute top-1 -left-6 hidden size-6 cursor-grab place-items-center rounded-md group-hover:grid"
            draggable
            onDragEnd={() => {
              dragId.current = null;
            }}
            onDragStart={(event) => {
              dragId.current = block.id;
              event.dataTransfer.effectAllowed = "move";
            }}
            type="button"
          >
            ⠿
          </button>
          {renderBlock(block)}
          <div className="absolute top-1 right-0 flex gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
            {block.text.trim() ? (
              <Popover
                onOpenChange={(open) => setFormatFor(open ? block.id : null)}
                open={formatFor === block.id}
              >
                <PopoverTrigger asChild>
                  <button
                    aria-label={t("knowledge.formatBlock")}
                    className="text-chat-muted hover:bg-primary/5 grid size-6 place-items-center rounded-md"
                    type="button"
                  >
                    <Type className="size-3.5" />
                  </button>
                </PopoverTrigger>
                <PopoverContent align="start" className="w-56 p-2">
                  <div className="grid grid-cols-3 gap-1">
                    {FORMAT_ITEMS.map((item) => (
                      <button
                        className="hover:bg-primary/5 flex h-8 items-center rounded-md px-1.5 text-left text-xs"
                        key={item.type}
                        onClick={() => changeType(block.id, item.type)}
                        type="button"
                      >
                        {t(item.labelKey)}
                      </button>
                    ))}
                  </div>
                  <div className="border-chat-hairline my-1 border-t" />
                  <div className="grid grid-cols-3 gap-1">
                    <MenuButton onClick={() => indent(block, 1)}>
                      {t("knowledge.indentIncrease")}
                    </MenuButton>
                    <MenuButton onClick={() => indent(block, -1)}>
                      {t("knowledge.indentDecrease")}
                    </MenuButton>
                    <MenuButton onClick={() => patchBlock(block.id, { align: "left" })}>
                      {t("knowledge.alignLeft")}
                    </MenuButton>
                    <MenuButton onClick={() => patchBlock(block.id, { align: "center" })}>
                      {t("knowledge.alignCenter")}
                    </MenuButton>
                    <MenuButton onClick={() => patchBlock(block.id, { align: "right" })}>
                      {t("knowledge.alignRight")}
                    </MenuButton>
                  </div>
                  <div className="border-chat-hairline my-1 border-t" />
                  <div className="grid grid-cols-3 gap-1">
                    <MenuButton onClick={() => void copyBlock(block)}>
                      {t("knowledge.copy")}
                    </MenuButton>
                    <MenuButton
                      onClick={() => {
                        void copyBlock(block);
                        removeBlock(block.id);
                      }}
                    >
                      {t("knowledge.cut")}
                    </MenuButton>
                    <MenuButton destructive onClick={() => removeBlock(block.id)}>
                      {t("knowledge.delete")}
                    </MenuButton>
                  </div>
                </PopoverContent>
              </Popover>
            ) : (
              <Popover
                onOpenChange={(open) => {
                  setInsertFor(open ? block.id : null);
                  if (!open) setInsertQuery("");
                }}
                open={insertFor === block.id}
              >
                <PopoverTrigger asChild>
                  <button
                    aria-label={t("knowledge.insertBlock")}
                    className="text-chat-muted hover:bg-primary/5 grid size-6 place-items-center rounded-md"
                    type="button"
                  >
                    <Plus className="size-3.5" />
                  </button>
                </PopoverTrigger>
                <PopoverContent align="start" className="w-56 p-2">
                  <Input
                    className="mb-1 h-7 text-xs"
                    onChange={(event) => setInsertQuery(event.target.value)}
                    placeholder={t("knowledge.searchBlockType")}
                    value={insertQuery}
                  />
                  <div className="max-h-80 overflow-y-auto">
                    {filteredInsert.map((item) => (
                      <button
                        className="hover:bg-primary/5 flex h-8 w-full items-center rounded-md px-2 text-left text-xs"
                        key={item.type}
                        onClick={() => insertAfter(block.id, item.type)}
                        type="button"
                      >
                        {t(item.labelKey)}
                      </button>
                    ))}
                  </div>
                </PopoverContent>
              </Popover>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function EditableBlock({
  block,
  className,
  onBlur,
  onInput,
  onKeyDown,
  register,
}: {
  block: Block;
  className: string;
  onBlur: () => void;
  onInput: (text: string) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => void;
  register: (element: HTMLDivElement | null) => void;
}) {
  const t = useT();
  const hydrate = (element: HTMLDivElement | null) => {
    if (!element) {
      register(null);
      return;
    }
    if (element.dataset.hydrated !== "1") {
      element.textContent = block.type === "divider" ? "" : block.text;
      element.dataset.hydrated = "1";
    }
    register(element);
  };
  return (
    <div
      aria-label={t("knowledge.editorBlock")}
      aria-multiline="true"
      className={className}
      contentEditable
      data-block-id={block.id}
      onBlur={onBlur}
      onInput={(event) => onInput(readBlockText(event.currentTarget))}
      onKeyDown={onKeyDown}
      ref={hydrate}
      role="textbox"
      style={{ textAlign: block.align ?? "left" }}
      suppressContentEditableWarning
    />
  );
}

function MenuButton({
  children,
  onClick,
  destructive,
}: {
  children: ReactNode;
  onClick: () => void;
  destructive?: boolean;
}) {
  return (
    <button
      className={`hover:bg-primary/5 flex h-7 items-center justify-center rounded-md px-1 text-xs ${
        destructive ? "text-destructive" : ""
      }`}
      onClick={onClick}
      type="button"
    >
      {children}
    </button>
  );
}

function parseTableRows(text: string): string[][] {
  const lines = text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("|"));
  const data = lines.filter((line) => !/^\|[\s:|-]+\|$/.test(line));
  const rows = data.map((line) =>
    line
      .split("|")
      .slice(1, -1)
      .map((cell) => cell.trim()),
  );
  if (rows.length === 0) return [["列 1", "列 2"], ["", ""]];
  const width = Math.max(...rows.map((row) => row.length));
  return rows.map((row) => [...row, ...Array(width - row.length).fill("")]);
}

function serializeTableRows(rows: string[][]): string {
  const header = rows[0] ?? ["列 1"];
  const separator = header.map(() => "---");
  const body = rows.slice(1);
  const format = (cells: string[]) => `| ${cells.join(" | ")} |`;
  return [format(header), format(separator), ...body.map(format)].join("\n");
}

function TableBlock({ block, onText }: { block: Block; onText: (text: string) => void }) {
  const t = useT();
  const rows = useMemo(() => parseTableRows(block.text), [block.text]);

  const commit = (next: string[][]) => onText(serializeTableRows(next));
  const setCell = (rowIndex: number, colIndex: number, value: string) => {
    const next = rows.map((row) => [...row]);
    next[rowIndex][colIndex] = value;
    commit(next);
  };

  return (
    <div className="border-primary/10 my-2 overflow-x-auto rounded-lg border">
      <table className="w-full border-collapse text-xs">
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr className="border-b border-[var(--divider)]" key={rowIndex}>
              {row.map((cell, colIndex) => (
                <td className="p-0" key={colIndex}>
                  <input
                    aria-label={`${t("knowledge.tableCell")} ${rowIndex + 1}-${colIndex + 1}`}
                    className={`h-8 w-full bg-transparent px-2 outline-none ${
                      rowIndex === 0 ? "font-medium" : ""
                    }`}
                    onChange={(event) => setCell(rowIndex, colIndex, event.target.value)}
                    value={cell}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex gap-2 border-t border-[var(--divider)] px-2 py-1">
        <button
          className="text-chat-muted hover:bg-primary/5 rounded px-2 py-0.5 text-xs"
          onClick={() => commit([...rows, rows[0].map(() => "")])}
          type="button"
        >
          {t("knowledge.addRow")}
        </button>
        <button
          className="text-chat-muted hover:bg-primary/5 rounded px-2 py-0.5 text-xs"
          onClick={() => commit(rows.map((row) => [...row, ""]))}
          type="button"
        >
          {t("knowledge.addColumn")}
        </button>
        {rows.length > 2 ? (
          <button
            className="text-chat-muted hover:bg-primary/5 rounded px-2 py-0.5 text-xs"
            onClick={() => commit(rows.slice(0, -1))}
            type="button"
          >
            {t("knowledge.removeRow")}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function MediaBlock({
  block,
  onText,
  onUpload,
}: {
  block: Block;
  onText: (text: string) => void;
  onUpload: (file: File) => Promise<{ name: string; url: string }>;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [alt, url] = block.text.includes("|") ? block.text.split("|") : ["", ""];

  const pick = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy(true);
    try {
      const asset = await onUpload(file);
      onText(`${asset.name}|${asset.url}`);
    } finally {
      setBusy(false);
    }
  };

  const accept =
    block.type === "image"
      ? "image/png,image/jpeg,image/gif,image/webp,image/svg+xml"
      : block.type === "video"
        ? "video/mp4,video/webm"
        : undefined;

  return (
    <div className="border-primary/10 bg-primary/[0.02] my-1 rounded-lg border p-3">
      {url ? (
        block.type === "image" ? (
          <img alt={alt} className="max-h-80 rounded-md" src={url} />
        ) : block.type === "video" ? (
          <video className="max-h-80 w-full rounded-md" controls src={url}>
            <track kind="captions" />
          </video>
        ) : (
          <a className="text-chat-ink text-sm underline" download href={url}>
            {alt || url}
          </a>
        )
      ) : (
        <p className="text-chat-muted text-xs">{t("knowledge.mediaEmpty")}</p>
      )}
      <div className="mt-2 flex items-center gap-2">
        <button
          aria-label={t("knowledge.upload")}
          className="bg-primary/5 text-chat-ink hover:bg-primary/10 rounded-md px-2 py-1 text-xs"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
          type="button"
        >
          {busy ? t("knowledge.uploading") : url ? t("knowledge.replaceFile") : t("knowledge.uploadFile")}
        </button>
        {url ? <span className="text-chat-muted truncate text-[11px]">{alt}</span> : null}
      </div>
      <input accept={accept} className="hidden" onChange={pick} ref={inputRef} type="file" />
    </div>
  );
}

function CodeBlock({
  block,
  onChange,
  onChangeLanguage,
}: {
  block: Block;
  onChange: (text: string) => void;
  onChangeLanguage: (language: string) => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(block.text);
  useEffect(() => setDraft(block.text), [block.text]);
  const commit = () => {
    onChange(draft);
    setEditing(false);
  };
  if (editing) {
    return (
      <div className="border-primary/10 my-2 rounded-lg border">
        <div className="flex items-center gap-2 border-b border-[var(--divider)] px-2 py-1">
          <Input
            aria-label={t("knowledge.codeLanguage")}
            className="h-6 w-28 text-[11px]"
            onChange={(event) => onChangeLanguage(event.target.value)}
            placeholder="language"
            value={block.language ?? ""}
          />
          <button
            className="text-chat-muted hover:text-chat-ink ml-auto text-[11px] underline"
            onClick={commit}
            type="button"
          >
            {t("knowledge.done")}
          </button>
        </div>
        <textarea
          autoFocus
          className="bg-primary/5 min-h-32 w-full resize-y p-3 font-mono text-xs outline-none"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) commit();
          }}
          value={draft}
        />
      </div>
    );
  }
  return (
    <div className="border-primary/10 my-2 rounded-lg border">
      <div className="flex items-center border-b border-[var(--divider)] px-2 py-1">
        <span className="text-chat-muted text-[11px]">{block.language || "text"}</span>
        <button
          className="text-chat-muted hover:text-chat-ink ml-auto text-[11px] underline"
          onClick={() => setEditing(true)}
          type="button"
        >
          {t("knowledge.edit")}
        </button>
      </div>
      <pre className="bg-primary/5 overflow-x-auto p-3 font-mono text-xs whitespace-pre-wrap">
        {highlightCode(block.text)}
      </pre>
    </div>
  );
}

function DiagramBlock({ block, onChange }: { block: Block; onChange: (text: string) => void }) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(block.text);
  useEffect(() => setDraft(block.text), [block.text]);
  if (editing) {
    return (
      <div className="border-primary/10 my-2 rounded-lg border">
        <div className="flex items-center border-b border-[var(--divider)] px-2 py-1">
          <span className="text-chat-muted text-[11px]">mermaid</span>
          <button
            className="text-chat-muted hover:text-chat-ink ml-auto text-[11px] underline"
            onClick={() => {
              onChange(draft);
              setEditing(false);
            }}
            type="button"
          >
            {t("knowledge.done")}
          </button>
        </div>
        <textarea
          autoFocus
          className="bg-primary/5 min-h-32 w-full resize-y p-3 font-mono text-xs outline-none"
          onChange={(event) => setDraft(event.target.value)}
          value={draft}
        />
      </div>
    );
  }
  return (
    <div className="my-2">
      <DiagramCard code={block.text} />
      <button
        className="text-chat-muted hover:text-chat-ink mt-1 text-[11px] underline"
        onClick={() => setEditing(true)}
        type="button"
      >
        {t("knowledge.edit")}
      </button>
    </div>
  );
}

function ColumnsBlock({ block, onChange }: { block: Block; onChange: (text: string) => void }) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(block.text);
  useEffect(() => setDraft(block.text), [block.text]);
  const columns = block.text.split("\n---\n").slice(0, 3);
  if (editing) {
    return (
      <div className="border-primary/10 my-2 rounded-lg border">
        <div className="flex items-center border-b border-[var(--divider)] px-2 py-1">
          <span className="text-chat-muted text-[11px]">{t("knowledge.block.columns")}</span>
          <button
            className="text-chat-muted hover:text-chat-ink ml-auto text-[11px] underline"
            onClick={() => {
              onChange(draft);
              setEditing(false);
            }}
            type="button"
          >
            {t("knowledge.done")}
          </button>
        </div>
        <textarea
          autoFocus
          className="bg-primary/5 min-h-32 w-full resize-y p-3 font-mono text-xs outline-none"
          onChange={(event) => setDraft(event.target.value)}
          placeholder={t("knowledge.columnsHint")}
          value={draft}
        />
      </div>
    );
  }
  return (
    <div className="my-2">
      <div
        className="grid gap-3"
        style={{ gridTemplateColumns: `repeat(${Math.max(1, columns.length)}, minmax(0, 1fr))` }}
      >
        {columns.map((column, index) => (
          <div className="bg-primary/5 rounded-md p-3 text-sm whitespace-pre-wrap" key={index}>
            {column}
          </div>
        ))}
      </div>
      <button
        className="text-chat-muted hover:text-chat-ink mt-1 text-[11px] underline"
        onClick={() => setEditing(true)}
        type="button"
      >
        {t("knowledge.edit")}
      </button>
    </div>
  );
}

const TOKEN_RE =
  /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`|\b(?:const|let|var|function|return|if|else|for|while|class|import|from|export|def|async|await|try|except|raise|public|private|interface|type|struct|fn|use|package|SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|JOIN|NULL|True|False|None|true|false|null)\b|\b\d+(?:\.\d+)?\b)/g;

function highlightCode(code: string): ReactNode[] {
  const output: ReactNode[] = [];
  let cursor = 0;
  for (const match of code.matchAll(TOKEN_RE)) {
    const start = match.index ?? 0;
    if (start > cursor) output.push(code.slice(cursor, start));
    const token = match[0];
    const className = token.startsWith("//") || token.startsWith("#") || token.startsWith("/*")
      ? "text-chat-muted/70 italic"
      : token.startsWith('"') || token.startsWith("'") || token.startsWith("`")
        ? "text-chat-muted"
        : /^\d/.test(token)
          ? "text-chat-ink"
          : "text-chat-ink font-semibold";
    output.push(
      <span className={className} key={`${start}-${token.length}`}>
        {token}
      </span>,
    );
    cursor = start + token.length;
  }
  if (cursor < code.length) output.push(code.slice(cursor));
  return output;
}

function blockClassName(type: BlockType): string {
  const base =
    "min-h-8 rounded-md px-2 py-1 text-sm leading-7 whitespace-pre-wrap outline-none";
  switch (type) {
    case "h1":
      return `${base} text-2xl font-semibold`;
    case "h2":
      return `${base} text-xl font-semibold`;
    case "h3":
      return `${base} text-lg font-semibold`;
    case "h4":
      return `${base} text-base font-semibold`;
    case "h5":
      return `${base} text-sm font-semibold`;
    case "h6":
      return `${base} text-xs font-semibold uppercase`;
    case "quote":
      return `${base} border-primary/20 text-chat-muted border-l-2 pl-3`;
    case "highlight":
      return `${base} bg-yellow-100/70`;
    case "divider":
      return `${base} border-b border-[var(--divider)]`;
    case "table":
    case "columns":
      return `${base} bg-primary/5 font-mono text-xs whitespace-pre-wrap`;
    case "todo":
      return `${base} pl-7`;
    case "ul":
      return `${base} pl-6 list-disc`;
    case "ol":
      return `${base} pl-6 list-decimal`;
    case "frontmatter":
      return `${base} text-chat-muted bg-primary/5 font-mono text-xs whitespace-pre-wrap`;
    default:
      return base;
  }
}
