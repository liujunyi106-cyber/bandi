/**
 * 功能：SOP 可视化编辑器（ReactFlow 画布 + 节点/连线属性 + 前端校验 + 保存/发布）。
 * 编辑对象为资源中心的 .card.json；保存走 PUT /api/resources/sop/{id}。
 */
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  ArrowLeft,
  CheckCircle2,
  LayoutGrid,
  Plus,
  Save,
  Send,
  ShieldCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
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
import { cn } from "@/lib/utils";
import {
  buildFlowEdges,
  buildFlowNodes,
  layoutSop,
  parseSop,
  renameNode,
  serializeSop,
  setNodePosition,
  validateSop,
  type SopCard,
  type SopEdge,
  type SopNode,
  type SopNodeData,
  type SopValidationError,
} from "@/lib/sop-graph";
import { resourcesApi } from "@/services/resources";

import { SopInspector, type SopSelection } from "./SopInspector";
import { SopNode as SopNodeView } from "./SopNodeCard";
import { SopReworkEdge } from "./SopReworkEdge";

const SOP_NODE_TYPES = { sop: SopNodeView };
const SOP_EDGE_TYPES = { rework: SopReworkEdge };

export interface SopEditorProps {
  resourceId: string;
  onBack: () => void;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function uniqueNodeId(card: SopCard, base: string): string {
  const used = new Set(card.nodes.map((node) => node.node_id));
  if (!used.has(base)) return base;
  let index = 2;
  while (used.has(`${base}_${index}`)) index += 1;
  return `${base}_${index}`;
}

function emptyNode(card: SopCard, type: string): SopNode {
  const nodeId = uniqueNodeId(card, type === "review" ? "review" : "step");
  return {
    node_id: nodeId,
    name: type === "review" ? "审核" : "新步骤",
    type,
    instruction: "",
    optional: false,
    condition: null,
    expected_user_info: [],
    allowed_actions: [],
    knowledge_scope: {},
    retry_policy: {},
    metadata: {},
  };
}

export function SopEditor({ resourceId, onBack }: SopEditorProps) {
  const [card, setCard] = useState<SopCard | null>(null);
  const [saved, setSaved] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [errors, setErrors] = useState<SopValidationError[]>([]);
  const [selected, setSelected] = useState<SopSelection>(null);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<SopNodeData>>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    resourcesApi
      .detail("sop", resourceId)
      .then((payload) => {
        if (cancelled) return;
        const parsed = parseSop(payload.content);
        const positions = layoutSop(parsed);
        setCard(parsed);
        setSaved(serializeSop(parsed));
        setNodes(buildFlowNodes(parsed, positions));
        setEdges(buildFlowEdges(parsed));
        setErrors([]);
        setSelected(
          parsed.start_node_id
            ? { kind: "node", id: parsed.start_node_id }
            : parsed.nodes[0]
              ? { kind: "node", id: parsed.nodes[0].node_id }
              : null,
        );
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
  }, [resourceId, setEdges, setNodes]);

  const syncFlow = useCallback(
    (next: SopCard) => {
      const positions = layoutSop(next);
      setNodes((currentNodes) => {
        const currentPositions = Object.fromEntries(
          currentNodes.map((node) => [node.id, node.position]),
        );
        return buildFlowNodes(next, { ...positions, ...currentPositions });
      });
      setEdges(buildFlowEdges(next));
    },
    [setEdges, setNodes],
  );

  const updateCard = useCallback(
    (next: SopCard) => {
      setCard(next);
      syncFlow(next);
    },
    [syncFlow],
  );

  const dirty = Boolean(card && serializeSop(card) !== saved);

  const errorNodeIds = useMemo(
    () =>
      new Set(
        errors
          .map((item) => item.nodeId)
          .filter((nodeId): nodeId is string => Boolean(nodeId)),
      ),
    [errors],
  );

  const flowNodes = useMemo(
    () =>
      nodes.map((node) => {
        const hasError = errorNodeIds.has(node.id);
        return Boolean(node.data.error) === hasError
          ? node
          : { ...node, data: { ...node.data, error: hasError } };
      }),
    [errorNodeIds, nodes],
  );

  const updateNode = useCallback(
    (nodeId: string, patch: Partial<SopNode>) => {
      if (!card) return;
      updateCard({
        ...card,
        nodes: card.nodes.map((node) => (node.node_id === nodeId ? { ...node, ...patch } : node)),
      });
    },
    [card, updateCard],
  );

  const updateEdge = useCallback(
    (index: number, patch: Partial<SopEdge>) => {
      if (!card) return;
      updateCard({
        ...card,
        edges: card.edges.map((edge, edgeIndex) =>
          edgeIndex === index ? { ...edge, ...patch } : edge,
        ),
      });
    },
    [card, updateCard],
  );

  const cardWithPositions = useCallback(
    (base: SopCard | null = card): SopCard | null => {
      if (!base) return null;
      const positions = Object.fromEntries(nodes.map((node) => [node.id, node.position]));
      return {
        ...base,
        nodes: base.nodes.map((node) =>
          positions[node.node_id] ? setNodePosition(node, positions[node.node_id]) : node,
        ),
      };
    },
    [card, nodes],
  );

  const saveCard = useCallback(
    async (override?: SopCard): Promise<SopCard | null> => {
      const next = cardWithPositions(override ?? card);
      if (!next || saving) return null;
      const nextErrors = validateSop(next);
      setErrors(nextErrors);
      if (nextErrors.length > 0) {
        toast.error(`校验未通过：${nextErrors[0].message}`);
        return null;
      }
      setSaving(true);
      try {
        const text = serializeSop(next);
        await resourcesApi.save("sop", resourceId, text);
        setCard(next);
        setSaved(text);
        toast.success("已保存");
        return next;
      } catch (reason) {
        toast.error(`保存失败：${errorText(reason)}`);
        return null;
      } finally {
        setSaving(false);
      }
    },
    [card, cardWithPositions, resourceId, saving],
  );

  const publish = useCallback(async () => {
    const published = await saveCard();
    if (!published || publishing) return;
    setPublishing(true);
    try {
      await resourcesApi.setEnabled("sop", resourceId, true);
      const enabled = { ...published, enabled: true };
      setCard(enabled);
      setSaved(serializeSop(enabled));
      toast.success("已发布并启用");
    } catch (reason) {
      toast.error(`发布失败：${errorText(reason)}`);
    } finally {
      setPublishing(false);
    }
  }, [publishing, resourceId, saveCard]);

  const validate = useCallback(() => {
    if (!card) return;
    const nextErrors = validateSop(card);
    setErrors(nextErrors);
    if (nextErrors.length === 0) {
      toast.success("校验通过");
    } else {
      toast.error(`发现 ${nextErrors.length} 个问题`);
    }
  }, [card]);

  const addStep = useCallback(
    (type: "step" | "review") => {
      if (!card) return;
      const node = emptyNode(card, type);
      let nextEdges = card.edges;
      if (selected?.kind === "node" && !card.terminal_node_ids.includes(selected.id)) {
        nextEdges = [
          ...nextEdges,
          {
            source_node_id: selected.id,
            next_node_id: node.node_id,
            condition: null,
            priority: 0,
            label: null,
          },
        ];
      }
      const next = { ...card, nodes: [...card.nodes, node], edges: nextEdges };
      updateCard(next);
      setSelected({ kind: "node", id: node.node_id });
      toast.success(type === "review" ? "已添加审核节点" : "已添加步骤节点");
    },
    [card, selected, updateCard],
  );

  const deleteNode = useCallback(
    (nodeId: string) => {
      if (!card) return;
      if (card.start_node_id === nodeId || card.terminal_node_ids.includes(nodeId)) {
        toast.error("开始/结束节点不可删除");
        return;
      }
      const next = {
        ...card,
        nodes: card.nodes.filter((node) => node.node_id !== nodeId),
        edges: card.edges.filter(
          (edge) => edge.source_node_id !== nodeId && edge.next_node_id !== nodeId,
        ),
      };
      updateCard(next);
      setSelected(null);
    },
    [card, updateCard],
  );

  const deleteEdge = useCallback(
    (index: number) => {
      if (!card) return;
      const next = { ...card, edges: card.edges.filter((_, edgeIndex) => edgeIndex !== index) };
      updateCard(next);
      setSelected(null);
    },
    [card, updateCard],
  );

  const renameNodeById = useCallback(
    (oldId: string, newId: string): boolean => {
      if (!card) return false;
      try {
        const next = renameNode(card, oldId, newId);
        if (next === card) return false;
        updateCard(next);
        setSelected({ kind: "node", id: newId });
        return true;
      } catch (reason) {
        toast.error(errorText(reason));
        return false;
      }
    },
    [card, updateCard],
  );

  const duplicateNode = useCallback(
    (nodeId: string) => {
      if (!card) return;
      const source = card.nodes.find((node) => node.node_id === nodeId);
      if (!source) return;
      const copy = { ...source, node_id: uniqueNodeId(card, `${nodeId}_copy`) };
      const next = { ...card, nodes: [...card.nodes, copy] };
      updateCard(next);
      setSelected({ kind: "node", id: copy.node_id });
      toast.success("已复制节点");
    },
    [card, updateCard],
  );

  const saveWithPatch = useCallback(async () => {
    if (!card) return;
    const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(card.version);
    if (!match) {
      toast.error("版本必须是三段式 semver");
      return;
    }
    const nextVersion = `${match[1]}.${match[2]}.${Number(match[3]) + 1}`;
    const saved = await saveCard({ ...card, version: nextVersion });
    if (saved) toast.success(`已保存并升级到 v${nextVersion}`);
  }, [card, saveCard]);

  const autoLayout = useCallback(() => {
    if (!card) return;
    const positions = layoutSop(card);
    const next = {
      ...card,
      nodes: card.nodes.map((node) =>
        positions[node.node_id] ? setNodePosition(node, positions[node.node_id]) : node,
      ),
    };
    setCard(next);
    setNodes(buildFlowNodes(next, positions));
    setEdges(buildFlowEdges(next));
    toast.success("已应用自动布局");
  }, [card, setEdges, setNodes]);

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!card || !connection.source || !connection.target) return;
      if (connection.source === connection.target) return;
      if (
        card.edges.some(
          (edge) =>
            edge.source_node_id === connection.source && edge.next_node_id === connection.target,
        )
      ) {
        return;
      }
      const nextEdge = {
        source_node_id: connection.source,
        next_node_id: connection.target,
        condition: null,
        priority: 0,
        label: null,
      };
      const next = { ...card, edges: [...card.edges, nextEdge] };
      updateCard(next);
      setSelected({ kind: "edge", index: next.edges.length - 1 });
    },
    [card, updateCard],
  );

  const onEdgesDelete = useCallback(
    (removed: Edge[]) => {
      if (!card) return;
      const indexes = removed
        .map((edge) => (edge.data as { index?: number } | undefined)?.index)
        .filter((index): index is number => typeof index === "number");
      if (indexes.length === 0) return;
      const next = { ...card, edges: card.edges.filter((_, index) => !indexes.includes(index)) };
      updateCard(next);
      setSelected(null);
    },
    [card, updateCard],
  );

  const handleBack = () => {
    if (dirty) {
      setLeaveOpen(true);
      return;
    }
    onBack();
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "s") return;
      event.preventDefault();
      void saveCard();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [saveCard]);

  if (loading) {
    return (
      <div className="text-chat-muted flex h-full items-center justify-center text-xs">
        正在加载 SOP…
      </div>
    );
  }
  if (error || !card) {
    return (
      <div className="flex h-full flex-col gap-4 px-8 pt-9">
        <Button variant="ghost" size="sm" className="text-chat-muted -ml-2" onClick={onBack}>
          <ArrowLeft />
          返回资源中心
        </Button>
        <p className="text-destructive text-xs">{error || "SOP 加载失败"}</p>
      </div>
    );
  }

  return (
    <div className="bg-chat-bg flex h-full min-h-0 flex-col">
      <header className="border-chat-hairline bg-card flex h-14 shrink-0 items-center gap-2 border-b px-3">
        <Button variant="ghost" size="sm" className="text-chat-muted" onClick={handleBack}>
          <ArrowLeft />
          资源中心
        </Button>
        <span className="text-chat-muted text-xs">/</span>
        <input
          className="text-chat-ink hover:bg-primary/5 focus:bg-primary/5 h-8 w-[150px] rounded-lg bg-transparent px-2 text-sm font-semibold outline-none"
          value={card.name}
          aria-label="SOP 名称"
          onChange={(event) => updateCard({ ...card, name: event.target.value })}
        />
        <span className="bg-primary/5 text-chat-muted rounded-full px-2 py-0.5 text-[10px]">
          v{card.version}
        </span>
        {card.business_domain ? (
          <span className="bg-primary/5 text-chat-muted rounded-full px-2 py-0.5 text-[10px]">
            {card.business_domain}
          </span>
        ) : null}
        <span className={cn("text-[10px]", dirty ? "text-amber-700" : "text-chat-muted")}>
          {dirty ? "有未保存修改" : "已保存"}
        </span>
        <span className="flex-1" />
        <Button variant="outline" size="sm" onClick={autoLayout}>
          <LayoutGrid />
          自动布局
        </Button>
        <Button variant="outline" size="sm" onClick={validate}>
          <ShieldCheck />
          校验
        </Button>
        <Button variant="outline" size="sm" disabled={saving || publishing} onClick={() => void saveCard()}>
          <Save />
          {saving ? "保存中…" : "保存"}
        </Button>
        <Button size="sm" disabled={saving || publishing} onClick={() => void publish()}>
          <Send />
          {publishing ? "发布中…" : "发布"}
        </Button>
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-[190px_minmax(0,1fr)_300px]">
        <aside className="border-chat-hairline bg-card overflow-auto border-r p-2.5">
          <p className="text-chat-muted px-1 pb-2 text-[9px] tracking-[0.08em] uppercase">节点库</p>
          <div className="flex flex-col gap-1.5">
            <button
              type="button"
              className="border-chat-hairline hover:bg-primary/5 text-chat-muted flex h-8 items-center gap-2 rounded-lg border px-2 text-[11px]"
              onClick={() => toast.info("SOP 已有唯一开始节点")}
            >
              <span className="bg-primary/5 grid size-5 place-items-center rounded-md">
                <CheckCircle2 className="size-3" />
              </span>
              开始
            </button>
            <button
              type="button"
              className="border-chat-hairline hover:bg-primary/5 text-chat-muted flex h-8 items-center gap-2 rounded-lg border px-2 text-[11px]"
              onClick={() => addStep("step")}
            >
              <span className="bg-primary/5 grid size-5 place-items-center rounded-md">
                <Plus className="size-3" />
              </span>
              步骤
            </button>
            <button
              type="button"
              className="border-chat-hairline hover:bg-primary/5 text-chat-muted flex h-8 items-center gap-2 rounded-lg border px-2 text-[11px]"
              onClick={() => addStep("review")}
            >
              <span className="bg-primary/5 grid size-5 place-items-center rounded-md">
                <ShieldCheck className="size-3" />
              </span>
              审核
            </button>
            <button
              type="button"
              className="border-chat-hairline hover:bg-primary/5 text-chat-muted flex h-8 items-center gap-2 rounded-lg border px-2 text-[11px]"
              onClick={() => toast.info("SOP 已有结束节点")}
            >
              <span className="bg-primary/5 grid size-5 place-items-center rounded-md">
                <CheckCircle2 className="size-3" />
              </span>
              结束
            </button>
          </div>

          <p className="text-chat-muted mt-4 px-1 pb-2 text-[9px] tracking-[0.08em] uppercase">大纲</p>
          <div className="flex flex-col">
            {card.nodes.map((node, index) => (
              <button
                key={node.node_id}
                type="button"
                onClick={() => setSelected({ kind: "node", id: node.node_id })}
                className={cn(
                  "hover:bg-primary/5 text-chat-muted flex items-center gap-2 rounded-md px-1.5 py-1.5 text-left text-[11px]",
                  selected?.kind === "node" && selected.id === node.node_id && "bg-primary/5 text-chat-ink",
                )}
              >
                <span className="bg-primary/5 grid size-4 shrink-0 place-items-center rounded-full text-[8px]">
                  {index + 1}
                </span>
                <span className="truncate">{node.name || node.node_id}</span>
              </button>
            ))}
          </div>
        </aside>

        <div
          className="min-h-0 overflow-hidden"
          style={{
            backgroundColor: "#f7f8fa",
            backgroundImage: "radial-gradient(#d9dde3 1px, transparent 1px)",
            backgroundSize: "16px 16px",
          }}
        >
          <ReactFlow
            nodes={flowNodes}
            edges={edges}
            nodeTypes={SOP_NODE_TYPES}
            edgeTypes={SOP_EDGE_TYPES}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onEdgesDelete={onEdgesDelete}
            onNodeClick={(_, node) => setSelected({ kind: "node", id: node.id })}
            onEdgeClick={(_, edge) => {
              const index = (edge.data as { index?: number } | undefined)?.index;
              if (typeof index === "number") setSelected({ kind: "edge", index });
            }}
            onNodeDragStop={(_, node) => {
              setCard((current) =>
                current
                  ? {
                      ...current,
                      nodes: current.nodes.map((item) =>
                        item.node_id === node.id ? setNodePosition(item, node.position) : item,
                      ),
                    }
                  : current,
              );
            }}
            onPaneClick={() => setSelected(null)}
            nodesConnectable
            edgesFocusable
            deleteKeyCode={["Backspace", "Delete"]}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            minZoom={0.3}
            maxZoom={1.5}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} size={1} color="#eef0f4" />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeStrokeWidth={2} className="!bg-card" />
          </ReactFlow>
        </div>

        <aside className="border-chat-hairline bg-card overflow-auto border-l p-3">
          <SopInspector
            card={card}
            selected={selected}
            onChangeCard={(patch) => updateCard({ ...card, ...patch })}
            onChangeNode={updateNode}
            onChangeEdge={updateEdge}
            onDeleteNode={deleteNode}
            onDeleteEdge={deleteEdge}
            onRenameNode={renameNodeById}
            onDuplicateNode={duplicateNode}
            onSelectEdge={(index) => setSelected({ kind: "edge", index })}
            onSaveWithPatch={() => void saveWithPatch()}
          />
        </aside>
      </div>

      <div
        className={cn(
          "flex h-10 shrink-0 items-center gap-2 border-t px-3 text-[11px]",
          errors.length > 0
            ? "border-[#fecaca] bg-[#fef2f2] text-destructive"
            : "border-chat-hairline bg-card text-[#138a55]",
        )}
      >
        {errors.length > 0 ? (
          <>
            <ShieldCheck className="size-3.5" />
            <span>{`${errors.length} 个问题：`}</span>
            <span className="flex min-w-0 gap-3 overflow-hidden whitespace-nowrap">
              {errors.slice(0, 2).map((item, index) => (
                <button
                  key={`${item.code}-${index}`}
                  type="button"
                  className="truncate underline underline-offset-2"
                  onClick={() =>
                    setSelected(
                      item.nodeId
                        ? { kind: "node", id: item.nodeId }
                        : item.edgeIndex !== undefined
                          ? { kind: "edge", index: item.edgeIndex }
                          : null,
                    )
                  }
                >
                  {item.message}
                </button>
              ))}
            </span>
          </>
        ) : (
          <>
            <CheckCircle2 className="size-3.5" />
            <span>{`校验通过 · ${card.nodes.length} 个节点 · ${card.edges.length} 条边`}</span>
          </>
        )}
        <span className="flex-1" />
        <span className="text-chat-muted text-[10px]">保存后立即对员工生效</span>
      </div>

      <AlertDialog open={leaveOpen} onOpenChange={setLeaveOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>离开编辑器？</AlertDialogTitle>
            <AlertDialogDescription>当前有未保存的修改，离开后会丢失。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>继续编辑</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              onClick={() => {
                setLeaveOpen(false);
                onBack();
              }}
            >
              放弃修改并返回
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
