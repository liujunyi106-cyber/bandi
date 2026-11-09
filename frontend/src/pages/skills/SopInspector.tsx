/**
 * 功能：SOP 编辑器右侧属性面板（节点 / 连线 / 卡级设置）。
 */
import { Copy, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import type { SopCard, SopEdge, SopNode } from "@/lib/sop-graph";

export type SopSelection =
  | { kind: "node"; id: string }
  | { kind: "edge"; index: number }
  | null;

export interface SopInspectorProps {
  card: SopCard;
  selected: SopSelection;
  onChangeCard: (patch: Partial<SopCard>) => void;
  onChangeNode: (nodeId: string, patch: Partial<SopNode>) => void;
  onChangeEdge: (index: number, patch: Partial<SopEdge>) => void;
  onDeleteNode: (nodeId: string) => void;
  onDeleteEdge: (index: number) => void;
  onRenameNode: (oldId: string, newId: string) => boolean;
  onDuplicateNode: (nodeId: string) => void;
  onSelectEdge: (index: number) => void;
  onSaveWithPatch: () => void;
}

function fieldClass() {
  return "border-chat-hairline bg-card focus:border-primary/40 h-8 w-full rounded-lg border px-2 text-xs outline-none";
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-chat-muted mb-1 block text-[10px]">{label}</span>
      {children}
    </label>
  );
}

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function NodeInspector({
  card,
  node,
  onChangeNode,
  onDeleteNode,
  onRenameNode,
  onDuplicateNode,
  onSelectEdge,
}: {
  card: SopCard;
  node: SopNode;
  onChangeNode: (nodeId: string, patch: Partial<SopNode>) => void;
  onDeleteNode: (nodeId: string) => void;
  onRenameNode: (oldId: string, newId: string) => boolean;
  onDuplicateNode: (nodeId: string) => void;
  onSelectEdge: (index: number) => void;
}) {
  const [retryText, setRetryText] = useState(JSON.stringify(node.retry_policy ?? {}, null, 2));
  const [retryError, setRetryError] = useState("");
  const [nodeIdText, setNodeIdText] = useState(node.node_id);
  const isStart = card.start_node_id === node.node_id;
  const isTerminal = card.terminal_node_ids.includes(node.node_id);
  const outgoing = card.edges
    .map((edge, index) => ({ edge, index }))
    .filter((item) => item.edge.source_node_id === node.node_id);

  useEffect(() => {
    setRetryText(JSON.stringify(node.retry_policy ?? {}, null, 2));
    setRetryError("");
  }, [node.node_id, node.retry_policy]);

  useEffect(() => {
    setNodeIdText(node.node_id);
  }, [node.node_id]);

  const commitNodeId = () => {
    const next = nodeIdText.trim();
    if (!next || next === node.node_id) {
      setNodeIdText(node.node_id);
      return;
    }
    if (!onRenameNode(node.node_id, next)) {
      setNodeIdText(node.node_id);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-chat-ink text-sm font-semibold">节点属性</h2>
          <code className="text-chat-muted font-mono text-[9px]">{node.node_id}</code>
        </div>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="复制节点"
            title="复制节点"
            onClick={() => onDuplicateNode(node.node_id)}
          >
            <Copy />
          </Button>
          <Button
            variant="destructive"
            size="icon-sm"
            aria-label="删除节点"
            title={isStart || isTerminal ? "开始/结束节点不可删除" : "删除节点"}
            disabled={isStart || isTerminal}
            onClick={() => onDeleteNode(node.node_id)}
          >
            <Trash2 />
          </Button>
        </div>
      </div>

      <Field label="名称">
        <input
          className={fieldClass()}
          value={node.name}
          onChange={(event) => onChangeNode(node.node_id, { name: event.target.value })}
        />
      </Field>
      <Field label="节点 ID">
        <input
          className={fieldClass()}
          value={nodeIdText}
          onChange={(event) => setNodeIdText(event.target.value)}
          onBlur={commitNodeId}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.currentTarget.blur();
            if (event.key === "Escape") {
              setNodeIdText(node.node_id);
              event.currentTarget.blur();
            }
          }}
        />
      </Field>
      <Field label="类型">
        <select
          className={fieldClass()}
          value={node.type}
          onChange={(event) => onChangeNode(node.node_id, { type: event.target.value })}
        >
          <option value="collect_info">收集信息</option>
          <option value="step">执行步骤</option>
          <option value="review">审核</option>
          <option value="notify">通知</option>
        </select>
      </Field>
      <Field label="执行指令">
        <textarea
          className="border-chat-hairline bg-card focus:border-primary/40 min-h-[72px] w-full resize-y rounded-lg border px-2 py-1.5 text-xs leading-5 outline-none"
          value={node.instruction}
          onChange={(event) => onChangeNode(node.node_id, { instruction: event.target.value })}
        />
      </Field>
      <Field label="进入条件（可选）">
        <input
          className={fieldClass()}
          value={node.condition ?? ""}
          placeholder="如：已收集到商品名称"
          onChange={(event) =>
            onChangeNode(node.node_id, { condition: event.target.value.trim() || null })
          }
        />
      </Field>
      <div className="flex items-center justify-between">
        <span className="text-chat-muted text-[11px]">可选节点（可跳过）</span>
        <Switch
          checked={node.optional}
          aria-label="可选节点"
          onCheckedChange={(checked) => onChangeNode(node.node_id, { optional: checked })}
        />
      </div>
      <Field label="需要收集的信息（每行一项）">
        <textarea
          className="border-chat-hairline bg-card focus:border-primary/40 min-h-[54px] w-full resize-y rounded-lg border px-2 py-1.5 text-xs leading-5 outline-none"
          value={node.expected_user_info.join("\n")}
          onChange={(event) =>
            onChangeNode(node.node_id, { expected_user_info: lines(event.target.value) })
          }
        />
      </Field>
      <Field label="允许工具（每行一项）">
        <textarea
          className="border-chat-hairline bg-card focus:border-primary/40 min-h-[54px] w-full resize-y rounded-lg border px-2 py-1.5 text-xs leading-5 outline-none"
          value={node.allowed_actions.join("\n")}
          onChange={(event) =>
            onChangeNode(node.node_id, { allowed_actions: lines(event.target.value) })
          }
        />
      </Field>
      {outgoing.length > 0 ? (
        <div>
          <p className="text-chat-muted mb-1 text-[10px]">出边</p>
          <div className="flex flex-col gap-1">
            {outgoing.map(({ edge, index }) => (
              <button
                key={`${edge.source_node_id}-${edge.next_node_id}-${index}`}
                type="button"
                data-testid={`node-edge-${index}`}
                className="border-chat-hairline hover:bg-primary/5 flex items-center justify-between gap-2 rounded-lg border px-2 py-1.5 text-left text-[10px]"
                onClick={() => onSelectEdge(index)}
              >
                <span className="text-chat-ink truncate font-mono">{edge.next_node_id}</span>
                <span className="text-chat-muted shrink-0">
                  {edge.label || edge.condition || "无条件"}
                </span>
              </button>
            ))}
          </div>
        </div>
      ) : null}
      <Field label="重试策略（JSON）">
        <textarea
          className="border-chat-hairline bg-card focus:border-primary/40 min-h-[54px] w-full resize-y rounded-lg border px-2 py-1.5 font-mono text-[10px] leading-5 outline-none"
          value={retryText}
          onChange={(event) => {
            setRetryText(event.target.value);
            setRetryError("");
          }}
          onBlur={() => {
            try {
              const parsed = JSON.parse(retryText || "{}");
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("必须是对象");
              }
              onChangeNode(node.node_id, { retry_policy: parsed as Record<string, unknown> });
            } catch {
              setRetryError("重试策略必须是 JSON 对象");
            }
          }}
        />
        {retryError ? <p className="mt-1 text-[10px] text-destructive">{retryError}</p> : null}
      </Field>
    </div>
  );
}

function EdgeInspector({
  edge,
  index,
  onChangeEdge,
  onDeleteEdge,
}: {
  edge: SopEdge;
  index: number;
  onChangeEdge: (index: number, patch: Partial<SopEdge>) => void;
  onDeleteEdge: (index: number) => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-chat-ink text-sm font-semibold">连线属性</h2>
          <code className="text-chat-muted font-mono text-[9px]">
            {edge.source_node_id} → {edge.next_node_id}
          </code>
        </div>
        <Button
          variant="destructive"
          size="icon-sm"
          aria-label="删除连线"
          onClick={() => onDeleteEdge(index)}
        >
          <Trash2 />
        </Button>
      </div>
      <Field label="转移标签">
        <input
          className={fieldClass()}
          value={edge.label ?? ""}
          placeholder="如：通过 / 驳回"
          onChange={(event) => onChangeEdge(index, { label: event.target.value || null })}
        />
      </Field>
      <Field label="条件（可选）">
        <input
          className={fieldClass()}
          value={edge.condition ?? ""}
          placeholder="如：审核通过"
          onChange={(event) => onChangeEdge(index, { condition: event.target.value || null })}
        />
      </Field>
      <Field label="优先级">
        <input
          className={fieldClass()}
          type="number"
          value={edge.priority}
          onChange={(event) => onChangeEdge(index, { priority: Number(event.target.value) || 0 })}
        />
      </Field>
      <p className="text-chat-muted text-[10px] leading-5">
        条件当前作为流程元数据保存；自动路由只走无条件的出边，按优先级选择。
      </p>
    </div>
  );
}

function CardInspector({
  card,
  onChangeCard,
  onSaveWithPatch,
}: {
  card: SopCard;
  onChangeCard: (patch: Partial<SopCard>) => void;
  onSaveWithPatch: () => void;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div>
        <h2 className="text-chat-ink text-sm font-semibold">SOP 设置</h2>
        <code className="text-chat-muted font-mono text-[9px]">{card.skill_id}</code>
      </div>
      <Field label="名称">
        <input
          className={fieldClass()}
          value={card.name}
          onChange={(event) => onChangeCard({ name: event.target.value })}
        />
      </Field>
      <Field label="版本">
        <input
          className={fieldClass()}
          value={card.version}
          onChange={(event) => onChangeCard({ version: event.target.value })}
        />
      </Field>
      <Field label="业务领域">
        <input
          className={fieldClass()}
          value={card.business_domain ?? ""}
          onChange={(event) => onChangeCard({ business_domain: event.target.value || null })}
        />
      </Field>
      <Field label="描述">
        <textarea
          className="border-chat-hairline bg-card focus:border-primary/40 min-h-[64px] w-full resize-y rounded-lg border px-2 py-1.5 text-xs leading-5 outline-none"
          value={card.description}
          onChange={(event) => onChangeCard({ description: event.target.value })}
        />
      </Field>
      <Field label="目标（每行一项）">
        <textarea
          className="border-chat-hairline bg-card focus:border-primary/40 min-h-[64px] w-full resize-y rounded-lg border px-2 py-1.5 text-xs leading-5 outline-none"
          value={card.goal.join("\n")}
          onChange={(event) => onChangeCard({ goal: lines(event.target.value) })}
        />
      </Field>
      <p className="text-chat-muted text-[10px] leading-5">
        选中画布节点或连线后，可编辑节点指令、工具与转移条件。
      </p>
      <Button variant="outline" size="sm" onClick={onSaveWithPatch}>
        保存并升 patch
      </Button>
    </div>
  );
}

export function SopInspector({
  card,
  selected,
  onChangeCard,
  onChangeNode,
  onChangeEdge,
  onDeleteNode,
  onDeleteEdge,
  onRenameNode,
  onDuplicateNode,
  onSelectEdge,
  onSaveWithPatch,
}: SopInspectorProps) {
  if (selected?.kind === "edge") {
    const edge = card.edges[selected.index];
    if (edge) {
      return (
        <EdgeInspector
          edge={edge}
          index={selected.index}
          onChangeEdge={onChangeEdge}
          onDeleteEdge={onDeleteEdge}
        />
      );
    }
  }
  if (selected?.kind === "node") {
    const node = card.nodes.find((item) => item.node_id === selected.id);
    if (node) {
      return (
        <NodeInspector
          card={card}
          node={node}
          onChangeNode={onChangeNode}
          onDeleteNode={onDeleteNode}
          onRenameNode={onRenameNode}
          onDuplicateNode={onDuplicateNode}
          onSelectEdge={onSelectEdge}
        />
      );
    }
  }
  return (
    <CardInspector
      card={card}
      onChangeCard={onChangeCard}
      onSaveWithPatch={onSaveWithPatch}
    />
  );
}
