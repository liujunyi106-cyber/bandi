/**
  * 功能：过程消息（）——无卡片
 * 字段行 + 多级折叠：总行（过程消息 · 状态 · 总耗时）→ 分类行（思考/联网搜索/调用 Skills/
 * 调用知识库/其他工具，×N）→ 明细行（具体项 + 耗时）；知识条目一行虚化 + 「展示」按钮 →
 * 约 100px 滚动内容区。执行中 spinner；完成灰色 ✓；进行中自动展开/完成 1.5s 自动收起。
 */
import {
  BookOpen,
  BookmarkCheck,
  Brain,
  ChevronRight,
  CircleSlash,
  FileSearch,
  FileText,
  Globe,
  History,
  Loader2,
  NotebookPen,
  Puzzle,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ProcessNode, ProcessNoteNode } from "@/stores/chat-store";
import {
  bucketFor,
  formatDurationMs,
  inputPreview,
  thinkingSummary,
  toolLabel,
  toolSummary,
  totalDurationMs,
  type BucketKey,
} from "./process-display";

const AUTO_COLLAPSE_MS = 1500;

interface BucketItem {
  id: string;
  text: string;
  /** 工具名（桶头按工具名聚合计数用，标本「资料搜索 ×7」）。 */
  toolName?: string;
  /** 知识全文（仅知识条目的「展示」按钮消费）。 */
  detail?: string;
  /**
   * 该行正文可折叠（思考节点「摘要 + 展开全文」两级）。
   * 折叠态只给摘要标题，正文进 max-h 封顶滚动区——长思考不淹没对话
    *。
   */
  collapsible?: boolean;
  durationMs?: number;
  status: "running" | "success" | "error" | "skipped";
  /** 后端 tool_call_id（-3b：失败项节点级重试定位；缺省 = 旧帧不可重试）。 */
  toolCallId?: string;
  /** 子代理嵌套子树（task 工具节点的 children）。 */
  children?: ProcessNode[];
}

interface Bucket {
  key: BucketKey;
  items: BucketItem[];
}

/** 功能：过程节点 → 分类桶列表（按首次出现顺序；skill 节点按技能拆条）。 */
function buildBuckets(nodes: ProcessNode[]): Bucket[] {
  const order: BucketKey[] = [];
  const map = new Map<BucketKey, BucketItem[]>();
  const push = (key: BucketKey, item: BucketItem) => {
    if (!map.has(key)) {
      map.set(key, []);
      order.push(key);
    }
    map.get(key)?.push(item);
  };
  for (const node of nodes) {
    if (node.kind === "thought") {
      // 分级：折叠态只给摘要标题（标本「思考完成 + 一句话转述」）
      // 原始 CoT 进展开区——避免长思考淹没对话
      // max-h-64 同思路）。摘要为规则提取，不调 LLM。
      const { title, body } = thinkingSummary(node.text);
      push("thought", {
        id: node.id,
        text: title || body,
        detail: title ? body : undefined,
        collapsible: Boolean(title && body),
        durationMs: node.durationMs,
        status: node.status,
      });
    } else if (node.kind === "skill") {
      for (const skill of node.skills) {
        push("skill", { id: `${node.id}:${skill}`, text: skill, status: "success" });
      }
    } else if (node.kind === "note") {
      continue;
    } else {
      const key = bucketFor(node.toolName);
      const scopeLabel = key === "knowledge" ? knowledgeScopeLabel(node.detail) : "";
      push(key, {
        id: node.id,
        text: `${toolSummary(node.toolName, node.input)}${scopeLabel ? `｜${scopeLabel}` : ""}`,
        toolName: node.toolName,
        detail: key === "knowledge" || key === "memory" ? node.detail : undefined,
        durationMs: node.durationMs,
        status: node.status,
        toolCallId: node.toolCallId,
        children: node.children,
      });
    }
  }
  return order.map((key) => ({ key, items: map.get(key) ?? [] }));
}

/** 功能：记忆写入注记按通道分档——每档一行，长期在上、草稿在下，各带图标。
 *
 * `long_term` 是用户理解的「长期记忆」；`working` 与 `compaction` 是本轮草稿与纪要。
 * 情景记忆（episodic）不进过程块——仅情景通道时整行不显示。无通道名（旧数据）回落总条数。
 */
interface MemoryRow {
  key: string;
  label: string;
  Icon: typeof Brain;
}

function memoryWriteRows(
  note: { count?: number; channels?: string[] },
  t: (key: string) => string,
): MemoryRow[] {
  const channels = note.channels;
  if (!channels || channels.length === 0) {
    return [
      {
        key: "written",
        label: t("chat.process.noteMemoryWritten").replace("{n}", String(note.count ?? 0)),
        Icon: Brain,
      },
    ];
  }
  const rows: MemoryRow[] = [];
  const longTerm = channels.filter((c) => c === "long_term").length;
  const scratch = channels.filter((c) => c === "working" || c === "compaction").length;
  if (longTerm > 0) {
    rows.push({
      key: "durable",
      label: t("chat.process.noteMemoryDurable").replace("{n}", String(longTerm)),
      Icon: BookmarkCheck,
    });
  }
  if (scratch > 0) {
    rows.push({
      key: "scratch",
      label: t("chat.process.noteMemoryScratch").replace("{n}", String(scratch)),
      Icon: NotebookPen,
    });
  }
  return rows;
}

/** 功能：过程注记文案（压缩/中断/断线/看门狗——统一灰色）。 */
function noteLabelKey(noteType: string): string {
  if (noteType === "compaction") return "chat.process.noteCompaction";
  if (noteType === "interrupt") return "chat.process.noteInterrupt";
  // 链路断开的结算注记
  if (noteType === "disconnected") return "chat.process.noteDisconnected";
  // 流看门狗本地收尾注记（超时未收到结束信号）
  if (noteType === "watchdog") return "chat.process.noteWatchdog";
  return "";
}

const BUCKET_META: Record<BucketKey, { icon: typeof Brain; labelKey: string }> = {
  // 设计统一：技能桶 Sparkles→Puzzle（与左栏「技能市场」同源）
  // 文件桶 FilePen→FileText（笔画更简）；全块描边由根节点 `[&_svg]:stroke-[1.75]` 统一
  thought: { icon: Brain, labelKey: "chat.process.thought" },
  web: { icon: Globe, labelKey: "chat.process.web" },
  skill: { icon: Puzzle, labelKey: "chat.process.skills" },
  knowledge: { icon: BookOpen, labelKey: "chat.process.knowledge" },
  memory: { icon: History, labelKey: "chat.process.memory" },
  explore: { icon: FileSearch, labelKey: "chat.process.explore" },
  file: { icon: FileText, labelKey: "chat.process.files" },
  tool: { icon: Wrench, labelKey: "chat.process.tools" },
};

/** 功能：明细行状态图标（执行中 spinner / 失败红 X / 已跳过灰 ⊘；成功不显示图标）。 */
function ItemStatus({ status }: { status: BucketItem["status"] }) {
  if (status === "running") {
    return <Loader2 className="text-primary size-3 shrink-0 animate-spin" />;
  }
  if (status === "error") return <X className="text-destructive size-3 shrink-0" />;
  if (status === "skipped") return <CircleSlash className="text-chat-muted/60 size-3 shrink-0" />;
  return null;
}

/** 功能：知识库检索范围（从结果摘要里取中文库名，展示「调用了哪个知识库」）。 */
function knowledgeScopeLabel(detail?: string): string {
  if (!detail) return "";
  const match = detail.match(/base_names["']?\s*[:=]\s*\[([^\]]*)\]/);
  if (!match) return "";
  const names = [...match[1].matchAll(/['"]([^'"]+)['"]/g)].map((item) => item[1]);
  return names.filter(Boolean).join("、");
}

/** 失败工具行 hover 操作回调（-3b：节点级重试 / 重发本轮 / 本地跳过；v5：点击行 → 详情）。 */
export interface ProcessBlockActions {
  onRetryTool?: (toolCallId: string) => void;
  onSkipTool?: (nodeId: string) => void;
  onResendTurn?: () => void;
  /** v5：点击工具明细行 → 右栏详情视图。 */
  onShowDetail?: (nodeId: string) => void;
}

/** 功能：知识条目——一行文本（后半虚化）+ 无边框「展示」按钮 → ~100px 滚动内容区（底部「收起」）。 */
function KnowledgeItem({ item }: { item: BucketItem }) {
  const t = useT();
  const [show, setShow] = useState(false);
  return (
    <div data-testid="process-knowledge-item">
      <div className="flex items-center gap-2 px-1 py-0.5 text-xs">
        <ItemStatus status={item.status} />
        <span
          className="text-chat-muted min-w-0 flex-1 truncate"
          style={{ maskImage: "linear-gradient(to right, #000 55%, transparent 100%)" }}
        >
          {item.text}
        </span>
        {item.detail ? (
          <button
            type="button"
            data-testid="process-knowledge-show"
            className="text-chat-muted hover:text-chat-ink shrink-0 text-[11px]"
            onClick={() => setShow((value) => !value)}
          >
            {show ? "" : t("chat.process.show")}
          </button>
        ) : null}
      </div>
      {show && item.detail ? (
        <div className="bg-chat-rail/40 text-chat-muted mt-0.5 mb-1 ml-5 rounded-lg px-2.5 py-2 text-xs leading-relaxed">
          <div className="max-h-[100px] overflow-y-auto whitespace-pre-wrap break-words">
            {item.detail}
          </div>
          <div className="flex justify-end pt-1">
            <button
              type="button"
              className="text-chat-muted hover:text-chat-ink text-[11px]"
              onClick={() => setShow(false)}
            >
              {t("chat.process.hide")}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** 功能：子代理嵌套树——task 节点下展开子代理内部过程（缩进 + 状态点）。 */
function SubagentTree({ nodes }: { nodes: ProcessNode[] }) {
  const t = useT();
  const [open, setOpen] = useState(true);
  const running = nodes.some((node) => node.status === "running");
  return (
    <div
      data-testid="process-subagent-tree"
      className="border-chat-rail/60 mt-0.5 ml-[18px] border-l pl-2.5"
    >
      <button
        type="button"
        data-testid="process-subagent-toggle"
        className="text-chat-muted hover:text-chat-ink flex items-center gap-1 py-0.5 text-[11px]"
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronRight className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")} />
        <span>{t("chat.process.subagent")}</span>
        {running ? <Loader2 className="text-primary size-3 shrink-0 animate-spin" /> : null}
      </button>
      {open ? (
        <div className="space-y-0.5">
          {nodes.map((node) => (
            <div
              key={node.id}
              data-testid="process-subagent-item"
              className="flex items-start gap-2 px-1 py-0.5 text-xs"
            >
              <span className="mt-[3px] flex shrink-0">
                <ItemStatus status={node.status} />
              </span>
              <span className="text-chat-muted min-w-0 flex-1 truncate">
                {subagentNodeText(node)}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** 功能：子代理节点行文案（思考摘要 / 工具名+输入 / 技能名 / 注记类型）。 */
function subagentNodeText(node: ProcessNode): string {
  if (node.kind === "thought") {
    const { title, body } = thinkingSummary(node.text);
    return title || body;
  }
  if (node.kind === "tool") {
    const preview = inputPreview(node.input);
    return preview ? `${node.toolName} · ${preview}` : node.toolName;
  }
  if (node.kind === "skill") return node.skills.join(", ");
  if (node.kind === "note") return node.noteType;
  return "";
}

/** 功能：分类行（状态左置与字号一致 + 图标 + 名称 + ×N + 折叠贴字段（仅完成展示）；点击展开明细）。 */
function BucketRow({ bucket, actions }: { bucket: Bucket; actions?: ProcessBlockActions }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  // 思考正文按行独立展开（摘要默认可见，原始 CoT 需手动展开）
  const [expandedItems, setExpandedItems] = useState<Record<string, boolean>>({});
  const running = bucket.items.some((item) => item.status === "running");
  // 按工具名聚合（桶内 items 是 BucketItem 而非 ProcessNode，直接就地聚合）
  const toolBreakdown = useMemo(() => {
    const out: { toolName: string; count: number }[] = [];
    const index = new Map<string, number>();
    for (const item of bucket.items) {
      if (!item.toolName) continue;
      const at = index.get(item.toolName);
      if (at === undefined) {
        index.set(item.toolName, out.length);
        out.push({ toolName: item.toolName, count: 1 });
      } else {
        out[at].count += 1;
      }
    }
    return out;
  }, [bucket.items]);
  const meta = BUCKET_META[bucket.key];
  const Icon = meta.icon;
  return (
    <div>
      <button
        type="button"
        data-testid={`process-bucket-${bucket.key}`}
        className="text-chat-muted flex w-full items-center gap-2 px-1 py-1 text-xs"
        onClick={() => setOpen((value) => !value)}
      >
        <ItemStatus status={running ? "running" : "success"} />
        <Icon className="size-3.5 shrink-0 opacity-75" />
        <span className="text-chat-ink">{t(meta.labelKey)}</span>
        {/* 按工具名聚合（标本「资料搜索 ×7」「设计准则查阅 ×2」）。
            规则计数，无 LLM 往返——opencode `contextToolSummary` 同口径。 */}
        <span data-testid={`process-bucket-count-${bucket.key}`} className="text-chat-muted">
          {toolBreakdown.length > 0 && bucket.items.length > 1
            ? toolBreakdown.map((g) => `${toolLabel(g.toolName)} ×${g.count}`).join("  ")
            : `x${bucket.items.length}`}
        </span>
        {!running ? (
          <ChevronRight
            data-testid={`process-bucket-toggle-${bucket.key}`}
            className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")}
          />
        ) : null}
      </button>
      {open ? (
        <div className="ml-[18px] space-y-0.5">
          {bucket.items.map((item) =>
            bucket.key === "knowledge" || bucket.key === "memory" ? (
              <KnowledgeItem key={item.id} item={item} />
            ) : (
              <div key={item.id}>
                <div
                  data-testid="process-item"
                  // 明细文本最多 3 行高、超出纵向滚动（不再单行省略）；
                  // 故行改顶部对齐，状态图标/耗时等元信息加 mt-[3px] 贴首行视觉居中
                  className={cn(
                    "group flex items-start gap-2 px-1 py-0.5 text-xs",
                    actions?.onShowDetail && item.toolCallId ? "cursor-pointer" : undefined,
                  )}
                  onClick={(event) => {
                    // v5：工具明细行点击 → 右栏详情视图（操作组按钮不触发）
                    if (!actions?.onShowDetail || !item.toolCallId) return;
                    if ((event.target as HTMLElement).closest("button")) return;
                    actions.onShowDetail(item.id);
                  }}
                >
                  <span data-testid="process-item-status" className="mt-[3px] flex shrink-0">
                    <ItemStatus status={item.status} />
                  </span>
                  <span
                    data-testid="process-item-text"
                    // 高度上限 = 3 行 × 19.5px（text-xs 12px × leading-relaxed 1.625）
                    className="text-chat-muted max-h-[58.5px] min-w-0 flex-1 overflow-y-auto leading-relaxed whitespace-pre-wrap break-words"
                  >
                    {item.text}
                  </span>
                  {item.collapsible && item.detail ? (
                    <button
                      type="button"
                      data-testid="process-item-expand"
                      aria-expanded={Boolean(expandedItems[item.id])}
                      aria-label={t("chat.process.show")}
                      className="text-chat-muted hover:text-chat-ink mt-[1px] shrink-0 text-[11px]"
                      onClick={(event) => {
                        event.stopPropagation();
                        setExpandedItems((prev) => ({ ...prev, [item.id]: !prev[item.id] }));
                      }}
                    >
                      {expandedItems[item.id] ? t("chat.process.hide") : t("chat.process.show")}
                    </button>
                  ) : null}
                  {item.status === "skipped" ? (
                    <span
                      data-testid="process-item-skipped"
                      className="text-chat-muted/60 mt-[3px] shrink-0 text-[11px]"
                    >
                      {t("chat.process.skipped")}
                    </span>
                  ) : null}
                  {/* 用户拍板：行尾不再常驻耗时（如 12ms）——
                      该位置本应承载「展开/收起」，但工具行的展开入口是点击行打开右侧详情
                      （ProcessBlockActions.onShowDetail），与思考正文的行内展开并非同一机制；
                      两种都做会给出两个含义相近的入口。故此处不显示，
                      耗时统一由总行「已完成 · 1.2s」承载。 */}
                </div>
                {item.children && item.children.length > 0 ? (
                  <SubagentTree nodes={item.children} />
                ) : null}
                {/* 思考正文二级展开：紧跟在本条之下（摘要默认可见，正文默认收起） */}
                {item.collapsible && item.detail && expandedItems[item.id] ? (
                  <div
                    data-testid="process-item-body"
                    className="text-chat-muted/90 ml-[26px] max-h-[220px] overflow-y-auto px-1 pb-1 text-xs leading-relaxed whitespace-pre-wrap break-words"
                  >
                    {item.detail}
                  </div>
                ) : null}
              </div>
            ),
          )}
        </div>
      ) : null}
    </div>
  );
}

export function ProcessBlock({
  nodes,
  actions,
  forceRunning = false,
  totalMsOverride,
}: {
  nodes: ProcessNode[];
  actions?: ProcessBlockActions;
  /** 功能：强制总行「正在执行」态（live 过程块用——过程节点全完成但回复卡片
   * 未落地时仍显示执行中，而非「已完成 · 时间」）。 */
  forceRunning?: boolean;
  /** 功能：总行时长的消息级覆盖——回复卡片落后的总执行时长（user 消息到回复
   * 落卡的墙钟差；缺省回退过程节点累计时长）。 */
  totalMsOverride?: number;
}) {
  const t = useT();
  // 终态注记（用户中断 / 链路断开）必须**折叠后仍可见** ——
  // 它们是这一轮的结局说明，藏在展开区里等于用户看不到（spec：过程块自动折叠后
  // 底部出现「用户中断」字段）。过程内注记（压缩中/记入记忆）保持在展开区内。
  const { insideNotes, terminalNotes } = useMemo(() => {
    const all = nodes.filter((node): node is ProcessNoteNode => node.kind === "note");
    const isTerminal = (note: ProcessNoteNode) =>
      note.noteType === "interrupt" ||
      note.noteType === "disconnected" ||
      note.noteType === "watchdog";
    return {
      insideNotes: all.filter((note) => !isTerminal(note)),
      terminalNotes: all.filter(isTerminal),
    };
  }, [nodes]);
  const buckets = useMemo(() => buildBuckets(nodes), [nodes]);
  const running = forceRunning || nodes.some((node) => node.status === "running");
  // 原先只累加 tool 节点 ⇒ 纯文本轮（无工具）恒显示「已完结 · 0.0s」
  //。改为工具 + 思考两类累加，无任何度量时显示「—」而非假精度 0.0s。
  const totalMs = useMemo(() => totalDurationMs(nodes), [nodes]);
  const [open, setOpen] = useState(running);
  const manualRef = useRef(false);

  useEffect(() => {
    if (running) {
      setOpen(true);
      manualRef.current = false;
      return;
    }
    if (manualRef.current) return;
    const timer = setTimeout(() => setOpen(false), AUTO_COLLAPSE_MS);
    return () => clearTimeout(timer);
  }, [running]);

  if (nodes.length === 0) return null;

  return (
    // w-full + min-w-0：三视图的父层皆为「flex flex-col items-start」，交叉轴 flex-start 会让
    // 本块按 fit-content 收缩，其下限 min-content 由内部长文本决定 ⇒ 过程块被顶宽右溢卡片
    // （894px vs 可用 508px）。明细文本虽已改多行滚动，钳宽仍需保留防回归。
    <div
      // [&_svg]:stroke-[1.75]：全块图标描边统一（CSS 覆盖 SVG 表现属性，lucide 默认 2 在
      // 12–14px 下偏重）——一处生效，覆盖桶图标 / 状态图标 / 折叠箭头
      // select-none：过程块整体禁止文本选中
      className="mb-1.5 w-full min-w-0 select-none [&_svg]:stroke-[1.75]"
      data-testid="process-block"
    >
      <button
        type="button"
        data-testid="process-toggle"
        className="text-chat-muted hover:text-chat-ink flex w-full items-center gap-2 rounded-md px-1 py-1 text-xs transition-colors"
        onClick={() => {
          manualRef.current = true;
          setOpen((value) => !value);
        }}
      >
        <ItemStatus status={running ? "running" : "success"} />
        {/* 总行不显示「过程消息」字段名，完成态只留「已完成 · 时间」；时长口径：
            优先消息级总执行时长（totalMsOverride——回复卡片落后的墙钟差），
            回退过程节点累计；为 0/缺失时不显示「· 时间」 */}
        <span className="text-chat-muted">
          {running
            ? t("chat.process.executing")
            : (totalMsOverride ?? totalMs) !== null && (totalMsOverride ?? totalMs)! > 0
              ? `${t("chat.process.completed")} · `
              : t("chat.process.completed")}
          <span data-testid="process-total-ms" className="tabular-nums">
            {running
              ? ""
              : totalMsOverride !== undefined
                ? formatDurationMs(totalMsOverride ?? null)
                : totalMs === null || totalMs <= 0
                  ? ""
                  : formatDurationMs(totalMs)}
          </span>
        </span>
        <ChevronRight className={cn("size-3 shrink-0 transition-transform", open && "rotate-90")} />
      </button>
      {open ? (
        <div className="mt-0.5 space-y-0.5">
          {insideNotes.map((note) => {
            const key = noteLabelKey(note.noteType);
            if (note.noteType === "memory_written") {
              const rows = memoryWriteRows(note, t);
              return (
                <div key={note.id} data-testid="process-note" data-memory-tier="true">
                  {rows.map((row) => (
                    <div
                      key={row.key}
                      data-testid="process-memory-tier"
                      data-tier={row.key}
                      className="text-chat-muted flex items-center gap-1.5 px-1 py-0.5 text-xs"
                    >
                      <row.Icon className="size-3 shrink-0" />
                      <span>{row.label}</span>
                    </div>
                  ))}
                </div>
              );
            }
            return (
              <div
                key={note.id}
                data-testid="process-note"
                className="text-chat-muted px-1 py-0.5 text-xs"
              >
                {key ? t(key) : note.noteType}
              </div>
            );
          })}
          {buckets.map((bucket) => (
            <BucketRow key={bucket.key} bucket={bucket} actions={actions} />
          ))}
        </div>
      ) : null}
      {/* 终态注记：折叠态也渲染（这一轮的结局说明，不得藏进展开区） */}
      {terminalNotes.length > 0 ? (
        <div data-testid="process-terminal-notes" className="mt-0.5">
          {terminalNotes.map((note) => (
            <div
              key={note.id}
              data-testid="process-note-terminal"
              className="text-chat-muted/80 px-1 py-0.5 text-xs"
            >
              {note.noteType === "interrupt"
                ? t("chat.process.noteInterrupt")
                : note.noteType === "watchdog"
                  ? t("chat.process.noteWatchdog")
                  : t("chat.process.noteDisconnected")}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
