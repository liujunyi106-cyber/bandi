/**
 * 功能：CSV 只读表格——七件套视图态（筛选 10 操作符/两级分组/多列排序/行高/字段与列宽/填色）。
 * 一期口径：不写回文件（编辑放三期）；添加一行禁用并标注。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/i18n";

export type CsvFilterOp =
  | "eq"
  | "neq"
  | "contains"
  | "not_contains"
  | "empty"
  | "not_empty"
  | "gt"
  | "lt"
  | "gte"
  | "lte";

export interface CsvFilter {
  col: number;
  op: CsvFilterOp;
  val: string;
}

export interface CsvSort {
  col: number;
  dir: "asc" | "desc";
}

export interface CsvViewState {
  filters?: CsvFilter[];
  groups?: number[];
  sorts?: CsvSort[];
  rowHeight?: number | "compact" | "default" | "roomy";
  fills?: Record<string, string>;
  hiddenCols?: number[];
  colWidths?: Record<string, number>;
}

const FILL_COLORS = [
  "#fef3c7",
  "#fee2e2",
  "#dcfce7",
  "#dbeafe",
  "#f3e8ff",
  "#fce7f3",
  "#e5e7eb",
  "#ffedd5",
];

interface ParsedCsv {
  headers: string[];
  rows: string[][];
}

/** 功能：RFC4180 解析（引号内逗号/换行/双引号转义），并剥掉 BOM。 */
function parseCsv(text: string): ParsedCsv {
  const input = text.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    if (quoted) {
      if (char === '"' && input[index + 1] === '"') {
        cell += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        cell += char;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else {
      cell += char;
    }
  }
  if (cell !== "" || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  const cleaned = rows.filter((entry) => entry.some((value) => value.trim() !== ""));
  if (cleaned.length === 0) return { headers: [], rows: [] };
  return { headers: cleaned[0], rows: cleaned.slice(1) };
}

function rowHeightOf(value: CsvViewState["rowHeight"]): number {
  if (typeof value === "number") return Math.max(20, Math.min(120, value));
  if (value === "compact") return 28;
  if (value === "roomy") return 48;
  return 36;
}

function compareValues(left: string, right: string): number {
  const leftNumber = Number(left);
  const rightNumber = Number(right);
  if (left !== "" && right !== "" && !Number.isNaN(leftNumber) && !Number.isNaN(rightNumber)) {
    return leftNumber - rightNumber;
  }
  return left.localeCompare(right, "zh-Hans-CN", { numeric: true });
}

function matches(cell: string, filter: CsvFilter): boolean {
  const value = filter.val;
  switch (filter.op) {
    case "eq":
      return cell === value;
    case "neq":
      return cell !== value;
    case "contains":
      return cell.includes(value);
    case "not_contains":
      return !cell.includes(value);
    case "empty":
      return cell === "";
    case "not_empty":
      return cell !== "";
    default: {
      const left = Number(cell);
      const right = Number(value);
      if (Number.isNaN(left) || Number.isNaN(right)) return false;
      if (filter.op === "gt") return left > right;
      if (filter.op === "lt") return left < right;
      if (filter.op === "gte") return left >= right;
      return left <= right;
    }
  }
}

export function CsvTable({
  text,
  initialState,
  onPersist,
}: {
  text: string;
  initialState: CsvViewState;
  onPersist: (state: CsvViewState) => void;
}) {
  const t = useT();
  const { headers, rows } = useMemo(() => parseCsv(text), [text]);
  const [state, setState] = useState<CsvViewState>(initialState);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selectedCell, setSelectedCell] = useState<{ row: number; col: number } | null>(null);
  const [fillScope, setFillScope] = useState<"cell" | "row" | "col">("cell");
  const [filterCol, setFilterCol] = useState(0);
  const [filterOp, setFilterOp] = useState<CsvFilterOp>("contains");
  const [filterVal, setFilterVal] = useState("");
  const [sortCol, setSortCol] = useState(0);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [groupCol, setGroupCol] = useState(0);
  const [heightDraft, setHeightDraft] = useState(String(rowHeightOf(initialState.rowHeight)));
  const timer = useRef<number | null>(null);

  useEffect(() => {
    setState(initialState);
    setHeightDraft(String(rowHeightOf(initialState.rowHeight)));
  }, [initialState]);

  const update = useCallback(
    (next: CsvViewState) => {
      setState(next);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => onPersist(next), 400);
    },
    [onPersist],
  );

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const filters = useMemo(() => state.filters ?? [], [state.filters]);
  const groups = useMemo(() => (state.groups ?? []).slice(0, 2), [state.groups]);
  const sorts = useMemo(() => state.sorts ?? [], [state.sorts]);
  const hidden = useMemo(() => new Set(state.hiddenCols ?? []), [state.hiddenCols]);
  const colWidths = state.colWidths ?? {};
  const fills = state.fills ?? {};
  const height = rowHeightOf(state.rowHeight);

  const visible = useMemo(() => {
    let view = rows.map((cells, index) => ({ cells, index }));
    for (const filter of filters) {
      view = view.filter(({ cells }) => matches(String(cells[filter.col] ?? ""), filter));
    }
    for (const sort of [...sorts].reverse()) {
      view = [...view].sort((left, right) => {
        const result = compareValues(
          String(left.cells[sort.col] ?? ""),
          String(right.cells[sort.col] ?? ""),
        );
        return sort.dir === "asc" ? result : -result;
      });
    }
    return view;
  }, [rows, filters, sorts]);

  const columns = headers.map((_, index) => index).filter((index) => !hidden.has(index));

  const addFilter = () => {
    update({ ...state, filters: [...filters, { col: filterCol, op: filterOp, val: filterVal }] });
    setFilterVal("");
  };
  const addSort = () => {
    const rest = sorts.filter((entry) => entry.col !== sortCol);
    update({ ...state, sorts: [...rest, { col: sortCol, dir: sortDir }] });
  };
  const addGroup = () => {
    if (groups.includes(groupCol) || groups.length >= 2) return;
    update({ ...state, groups: [...groups, groupCol] });
  };
  const applyFill = (color: string) => {
    if (!selectedCell) return;
    const next: Record<string, string> = { ...fills };
    const cellKey = `${selectedCell.row}:${selectedCell.col}`;
    if (fillScope === "cell") next[cellKey] = color;
    if (fillScope === "row") next[`${selectedCell.row}:*`] = color;
    if (fillScope === "col") next[`*:${selectedCell.col}`] = color;
    update({ ...state, fills: next });
  };
  const clearFill = () => {
    if (!selectedCell) return;
    const next = { ...fills };
    delete next[`${selectedCell.row}:${selectedCell.col}`];
    delete next[`${selectedCell.row}:*`];
    delete next[`*:${selectedCell.col}`];
    update({ ...state, fills: next });
  };
  const fillOf = (rowIndex: number, colIndex: number): string | undefined =>
    fills[`${rowIndex}:${colIndex}`] ?? fills[`${rowIndex}:*`] ?? fills[`*:${colIndex}`];

  const toggleGroup = (key: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderRow = (row: { cells: string[]; index: number }) => (
    <tr
      className="border-b border-[var(--divider)]"
      key={row.index}
      style={{ height }}
    >
      <td className="text-chat-muted px-2">{row.index + 1}</td>
      {columns.map((col) => {
        const selected = selectedCell?.row === row.index && selectedCell.col === col;
        const fill = fillOf(row.index, col);
        return (
          <td
            className={`max-w-[280px] cursor-cell truncate px-2 ${
              selected ? "ring-primary/30 ring-1 ring-inset" : ""
            }`}
            key={col}
            onClick={() => setSelectedCell({ row: row.index, col })}
            style={{
              backgroundColor: fill,
              maxWidth: colWidths[String(col)] ? `${colWidths[String(col)]}px` : undefined,
              width: colWidths[String(col)] ? `${colWidths[String(col)]}px` : undefined,
            }}
          >
            {row.cells[col] ?? ""}
          </td>
        );
      })}
    </tr>
  );

  const renderGroup = (
    bucket: { cells: string[]; index: number }[],
    level: number,
    parentKey: string,
  ) => {
    const col = groups[level];
    const key = `${parentKey}|${bucket[0]?.cells[col] ?? ""}`;
    const isCollapsed = collapsed.has(key);
    return (
      <>
        <tr
          className="bg-primary/5 cursor-pointer"
          key={`group-${key}`}
          onClick={() => toggleGroup(key)}
        >
          <td className="text-chat-muted px-2 py-1" colSpan={columns.length + 1}>
            {`${isCollapsed ? "▶" : "▼"} ${bucket[0]?.cells[col] ?? ""} · ${bucket.length}`}
          </td>
        </tr>
        {isCollapsed
          ? null
          : level + 1 < groups.length
            ? (() => {
                const map = new Map<string, { cells: string[]; index: number }[]>();
                for (const row of bucket) {
                  const groupKey = row.cells[groups[level + 1]] ?? "";
                  const list = map.get(groupKey) ?? [];
                  list.push(row);
                  map.set(groupKey, list);
                }
                return [...map.entries()].map(([groupKey, list]) => (
                  <FragmentGroup
                    bucket={list}
                    key={`${key}-${groupKey}`}
                    level={level + 1}
                    parentKey={key}
                    render={renderGroup}
                  />
                ));
              })()
            : bucket.map(renderRow)}
      </>
    );
  };

  const grouped = useMemo(() => {
    if (groups.length === 0) return null;
    const map = new Map<string, { cells: string[]; index: number }[]>();
    for (const row of visible) {
      const key = String(row.cells[groups[0]] ?? "");
      const bucket = map.get(key) ?? [];
      bucket.push(row);
      map.set(key, bucket);
    }
    return [...map.entries()];
  }, [groups, visible]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-1 border-b border-[var(--divider)] px-2 py-1.5">
        <Button
          className="text-chat-muted"
          disabled
          size="sm"
          title={t("csv.phase3")}
          variant="ghost"
        >
          {t("csv.addRow")}
        </Button>

        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("csv.fields")}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-56 p-2">
            {headers.map((header, index) => (
              <div className="flex items-center gap-2 py-1" key={index}>
                <input
                  aria-label={header}
                  checked={!hidden.has(index)}
                  className="accent-primary size-3"
                  onChange={() => {
                    const next = new Set(hidden);
                    if (next.has(index)) next.delete(index);
                    else next.add(index);
                    update({ ...state, hiddenCols: [...next] });
                  }}
                  type="checkbox"
                />
                <span className="min-w-0 flex-1 truncate text-xs">{header || `#${index + 1}`}</span>
                <Input
                  aria-label={`${header} 列宽`}
                  className="h-6 w-14 text-[11px]"
                  onChange={(event) => {
                    const value = Number(event.target.value);
                    const widths = { ...colWidths };
                    if (value > 0) widths[String(index)] = Math.min(600, value);
                    else delete widths[String(index)];
                    update({ ...state, colWidths: widths });
                  }}
                  placeholder={t("csv.width")}
                  value={colWidths[String(index)] ?? ""}
                />
              </div>
            ))}
          </PopoverContent>
        </Popover>

        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("csv.filter")}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-64 p-2">
            <div className="flex flex-col gap-1">
              <select
                className="border-chat-hairline h-8 rounded-md border px-2 text-xs"
                onChange={(event) => setFilterCol(Number(event.target.value))}
                value={filterCol}
              >
                {headers.map((header, index) => (
                  <option key={index} value={index}>
                    {header || `#${index + 1}`}
                  </option>
                ))}
              </select>
              <select
                className="border-chat-hairline h-8 rounded-md border px-2 text-xs"
                onChange={(event) => setFilterOp(event.target.value as CsvFilterOp)}
                value={filterOp}
              >
                <option value="eq">{t("csv.opEq")}</option>
                <option value="neq">{t("csv.opNeq")}</option>
                <option value="contains">{t("csv.opContains")}</option>
                <option value="not_contains">{t("csv.opNotContains")}</option>
                <option value="empty">{t("csv.opEmpty")}</option>
                <option value="not_empty">{t("csv.opNotEmpty")}</option>
                <option value="gt">{t("csv.opGt")}</option>
                <option value="lt">{t("csv.opLt")}</option>
                <option value="gte">{t("csv.opGte")}</option>
                <option value="lte">{t("csv.opLte")}</option>
              </select>
              <Input
                className="h-8 text-xs"
                onChange={(event) => setFilterVal(event.target.value)}
                placeholder={t("csv.value")}
                value={filterVal}
              />
              <Button onClick={addFilter} size="sm">
                {t("csv.addFilter")}
              </Button>
            </div>
          </PopoverContent>
        </Popover>

        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("csv.group")}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-56 p-2">
            <select
              className="border-chat-hairline mb-2 h-8 w-full rounded-md border px-2 text-xs"
              onChange={(event) => setGroupCol(Number(event.target.value))}
              value={groupCol}
            >
              {headers.map((header, index) => (
                <option key={index} value={index}>
                  {header || `#${index + 1}`}
                </option>
              ))}
            </select>
            <Button disabled={groups.length >= 2} onClick={addGroup} size="sm">
              {t("csv.addGroup")}
            </Button>
          </PopoverContent>
        </Popover>

        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("csv.sort")}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-56 p-2">
            <select
              className="border-chat-hairline mb-1 h-8 w-full rounded-md border px-2 text-xs"
              onChange={(event) => setSortCol(Number(event.target.value))}
              value={sortCol}
            >
              {headers.map((header, index) => (
                <option key={index} value={index}>
                  {header || `#${index + 1}`}
                </option>
              ))}
            </select>
            <select
              className="border-chat-hairline mb-2 h-8 w-full rounded-md border px-2 text-xs"
              onChange={(event) => setSortDir(event.target.value as "asc" | "desc")}
              value={sortDir}
            >
              <option value="asc">{t("csv.asc")}</option>
              <option value="desc">{t("csv.desc")}</option>
            </select>
            <Button onClick={addSort} size="sm">
              {t("csv.addSort")}
            </Button>
          </PopoverContent>
        </Popover>

        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("csv.rowHeight")}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-40 p-2">
            {[
              { key: 28 as const, label: t("csv.compact") },
              { key: 36 as const, label: t("csv.standard") },
              { key: 48 as const, label: t("csv.roomy") },
            ].map((item) => (
              <button
                className="hover:bg-primary/5 flex h-8 w-full items-center rounded-md px-2 text-xs"
                key={item.key}
                onClick={() => update({ ...state, rowHeight: item.key })}
                type="button"
              >
                {item.label}
              </button>
            ))}
            <Input
              className="mt-1 h-7 text-xs"
              onChange={(event) => setHeightDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key !== "Enter") return;
                const value = Number(heightDraft);
                if (value >= 20 && value <= 120) update({ ...state, rowHeight: value });
              }}
              placeholder={t("csv.customHeight")}
              value={heightDraft}
            />
          </PopoverContent>
        </Popover>

        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("csv.fill")}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-48 p-2">
            <div className="mb-2 flex gap-1">
              {(
                [
                  ["cell", t("csv.scopeCell")],
                  ["row", t("csv.scopeRow")],
                  ["col", t("csv.scopeCol")],
                ] as const
              ).map(([value, label]) => (
                <button
                  className={`h-7 flex-1 rounded-md text-[11px] ${
                    fillScope === value ? "bg-primary/10 text-chat-ink" : "text-chat-muted"
                  }`}
                  key={value}
                  onClick={() => setFillScope(value)}
                  type="button"
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="grid grid-cols-4 gap-1">
              {FILL_COLORS.map((color) => (
                <button
                  aria-label={color}
                  className="size-7 rounded-md border border-[var(--divider)]"
                  key={color}
                  onClick={() => applyFill(color)}
                  style={{ backgroundColor: color }}
                  type="button"
                />
              ))}
            </div>
            <Button
              className="mt-2 w-full"
              disabled={!selectedCell}
              onClick={clearFill}
              size="sm"
              variant="outline"
            >
              {t("csv.clearFill")}
            </Button>
          </PopoverContent>
        </Popover>
      </div>

      {filters.length > 0 || groups.length > 0 || sorts.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1 border-b border-[var(--divider)] px-2 py-1">
          {filters.map((filter, index) => (
            <Chip
              key={`f-${index}`}
              onRemove={() => update({ ...state, filters: filters.filter((_, i) => i !== index) })}
              text={`${headers[filter.col] ?? filter.col} ${opLabel(filter.op, t)} ${filter.val}`}
            />
          ))}
          {groups.map((col, index) => (
            <Chip
              key={`g-${col}`}
              onRemove={() =>
                update({ ...state, groups: groups.filter((_, i) => i !== index) })
              }
              text={`${t("csv.group")}: ${headers[col] ?? col}`}
            />
          ))}
          {sorts.map((sort, index) => (
            <Chip
              key={`s-${sort.col}`}
              onRemove={() => update({ ...state, sorts: sorts.filter((_, i) => i !== index) })}
              text={`${index + 1}. ${headers[sort.col] ?? sort.col} ${sort.dir === "asc" ? "↑" : "↓"}`}
            />
          ))}
          <button
            className="text-chat-muted hover:text-chat-ink text-[11px] underline"
            onClick={() => update({ ...state, filters: [], groups: [], sorts: [] })}
            type="button"
          >
            {t("csv.clearAll")}
          </button>
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-xs">
          <thead>
            <tr className="border-b border-[var(--divider)]">
              <th className="text-chat-muted w-10 px-2 py-1 text-left font-normal">#</th>
              {columns.map((col) => (
                <th
                  className="text-chat-muted px-2 py-1 text-left font-normal"
                  key={col}
                  style={
                    colWidths[String(col)] ? { width: `${colWidths[String(col)]}px` } : undefined
                  }
                >
                  {headers[col] || `#${col + 1}`}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grouped
              ? grouped.map(([groupKey, bucket]) => (
                  <FragmentGroup
                    bucket={bucket}
                    key={groupKey}
                    level={0}
                    parentKey=""
                    render={renderGroup}
                  />
                ))
              : visible.map(renderRow)}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FragmentGroup({
  bucket,
  level,
  parentKey,
  render,
}: {
  bucket: { cells: string[]; index: number }[];
  level: number;
  parentKey: string;
  render: (
    bucket: { cells: string[]; index: number }[],
    level: number,
    parentKey: string,
  ) => React.ReactNode;
}) {
  return <>{render(bucket, level, parentKey)}</>;
}

function Chip({ text, onRemove }: { text: string; onRemove: () => void }) {
  return (
    <span className="bg-primary/5 text-chat-muted inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]">
      {text}
      <button
        aria-label="移除"
        className="hover:text-chat-ink"
        onClick={onRemove}
        type="button"
      >
        ×
      </button>
    </span>
  );
}

function opLabel(op: CsvFilterOp, t: (key: string) => string): string {
  const keys: Record<CsvFilterOp, string> = {
    eq: "csv.opEq",
    neq: "csv.opNeq",
    contains: "csv.opContains",
    not_contains: "csv.opNotContains",
    empty: "csv.opEmpty",
    not_empty: "csv.opNotEmpty",
    gt: "csv.opGt",
    lt: "csv.opLt",
    gte: "csv.opGte",
    lte: "csv.opLte",
  };
  return t(keys[op]);
}
