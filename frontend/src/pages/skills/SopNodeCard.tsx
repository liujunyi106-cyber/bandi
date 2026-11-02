/**
 * 功能：SOP 画布节点（开始/步骤/审核/结束 + 可选标记 + 连线锚点）。
 */
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { CheckCircle2, FileText, Play, Wrench } from "lucide-react";

import { cn } from "@/lib/utils";
import type { SopNodeData } from "@/lib/sop-graph";

function NodeIcon({ type, isStart, isTerminal }: { type: string; isStart: boolean; isTerminal: boolean }) {
  if (isStart) return <Play className="size-3.5" aria-hidden="true" />;
  if (isTerminal) return <CheckCircle2 className="size-3.5" aria-hidden="true" />;
  if (type === "review") return <CheckCircle2 className="size-3.5" aria-hidden="true" />;
  if (type === "collect_info") return <FileText className="size-3.5" aria-hidden="true" />;
  return <Wrench className="size-3.5" aria-hidden="true" />;
}

/** 功能：SOP 节点卡片（纯展示层，可独立测试）。 */
export function SopNodeCard({ data, selected }: { data: SopNodeData; selected?: boolean }) {
  const { node, isStart, isTerminal, error } = data;
  return (
    <div
      className={cn(
        "group border-chat-hairline bg-card relative w-[220px] rounded-xl border px-3 py-2 shadow-sm transition-colors",
        selected && "border-primary",
        isStart && "border-l-primary border-l-[3px]",
        isTerminal && "border-l-[3px] border-l-[#138a55]",
        error && "border-destructive ring-destructive/15 ring-2",
      )}
      data-testid={`sop-node-${node.node_id}`}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="bg-primary/40! size-2! border-0! opacity-0 transition-opacity group-hover:opacity-100"
      />
      <div className="flex items-start gap-2">
        <span className="bg-primary/5 text-chat-muted mt-0.5 grid size-6 shrink-0 place-items-center rounded-lg">
          <NodeIcon type={node.type} isStart={isStart} isTerminal={isTerminal} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <p className="text-chat-ink truncate text-[11px] font-semibold">
              {node.name || node.node_id}
            </p>
            {node.optional ? (
              <span className="bg-primary/5 text-chat-muted rounded px-1 py-0.5 text-[8px]">
                可选
              </span>
            ) : null}
          </div>
          <p className="text-chat-muted mt-0.5 truncate text-[9px]">
            {node.instruction || "未填写执行指令"}
          </p>
        </div>
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="bg-primary/40! size-2! border-0! opacity-0 transition-opacity group-hover:opacity-100"
      />
    </div>
  );
}

export function SopNode({ data, selected }: NodeProps<Node<SopNodeData>>) {
  return <SopNodeCard data={data} selected={selected} />;
}
