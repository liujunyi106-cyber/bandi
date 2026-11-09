/**
 * 功能：知识库「图谱」页签——沿用原图谱渲染管线（拉取 → 名称过滤 → 规模收敛 → 布局 → SVG）。
 * 页头与搜索由上层 `pages/knowledge/index.tsx` 统一提供，本组件只负责画布与选中态。
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { EntityCard } from "@/components/graph/EntityCard";
import { GraphCanvas } from "@/components/graph/GraphCanvas";
import { GraphState } from "@/components/graph/GraphState";
import { buildTypeColorMap } from "@/components/graph/graph-visual";
import { useT } from "@/i18n";
import { layoutGraph } from "@/lib/graph-layout";
import {
  describeGraph,
  fetchGraph,
  filterSnapshot,
  selectRenderableSubgraph,
  type GraphEntity,
  type GraphSnapshot,
} from "@/services/graph";

type LoadState =
  | { status: "loading" }
  | { status: "error"; detail: string }
  | { status: "ready"; snapshot: GraphSnapshot };

export function GraphTab({ query }: { query: string }) {
  const t = useT();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [focusRequest, setFocusRequest] = useState<{ id: string; seq: number } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchGraph({ signal: controller.signal })
      .then((snapshot) => setState({ status: "ready", snapshot }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({
          status: "error",
          detail: error instanceof Error ? error.message : String(error),
        });
      });
    return () => controller.abort();
  }, [reloadToken]);

  const rawSnapshot = state.status === "ready" ? state.snapshot : null;
  const filtered = useMemo(
    () => (rawSnapshot ? filterSnapshot(rawSnapshot, query) : null),
    [query, rawSnapshot],
  );
  const renderable = useMemo(
    () => (filtered ? selectRenderableSubgraph(filtered.snapshot) : null),
    [filtered],
  );

  const snapshot = renderable?.snapshot ?? null;
  const summary = useMemo(() => (snapshot ? describeGraph(snapshot) : null), [snapshot]);
  const layout = useMemo(() => (snapshot ? layoutGraph(snapshot) : null), [snapshot]);
  const typeColors = useMemo(
    () => buildTypeColorMap(snapshot?.entities.map((entity) => entity.type) ?? []),
    [snapshot],
  );
  const entityById = useMemo(
    () => new Map(snapshot?.entities.map((entity) => [entity.id, entity]) ?? []),
    [snapshot],
  );
  const activeSelectedId = selectedId && entityById.has(selectedId) ? selectedId : null;

  const neighbors = useMemo<GraphEntity[]>(() => {
    if (!snapshot || !activeSelectedId) return [];
    const ids = new Set<string>();
    for (const edge of snapshot.edges) {
      if (edge.source === activeSelectedId) ids.add(edge.target);
      if (edge.target === activeSelectedId) ids.add(edge.source);
    }
    return [...ids]
      .map((id) => entityById.get(id))
      .filter((entity): entity is GraphEntity => Boolean(entity))
      .sort((left, right) => left.name.localeCompare(right.name, "zh-Hans-CN"));
  }, [activeSelectedId, entityById, snapshot]);

  const handleSelectAndLocate = useCallback((id: string) => {
    setSelectedId(id);
    setFocusRequest((current) => ({ id, seq: (current?.seq ?? 0) + 1 }));
  }, []);

  const handleRetry = useCallback(() => {
    setSelectedId(null);
    setReloadToken((token) => token + 1);
  }, []);

  const selectedEntity = activeSelectedId ? (entityById.get(activeSelectedId) ?? null) : null;
  const isLibraryEmpty =
    state.status === "ready" && rawSnapshot !== null && rawSnapshot.entities.length === 0;
  const isNoMatch =
    state.status === "ready" && snapshot !== null && snapshot.entities.length === 0 && !isLibraryEmpty;
  const queryActive = query.trim() !== "";
  const filterHidden = filtered ? filtered.hiddenEntityCount : 0;

  return (
    <div className="border-chat-hairline bg-card relative h-full min-h-0 overflow-hidden rounded-xl border">
      {state.status === "ready" && queryActive && filterHidden > 0 ? (
        <p className="border-border bg-card text-muted-foreground pointer-events-none absolute top-3 left-3 z-20 max-w-[min(520px,70%)] rounded-full border px-3 py-1.5 text-xs shadow-soft">
          {t("graph.filterNotice")}
          <span className="ml-2 tabular-nums">
            {`${t("graph.entityCount")} −${filterHidden} · ${t("graph.edgeCount")} −${filtered?.hiddenEdgeCount ?? 0}`}
          </span>
        </p>
      ) : null}
      {state.status === "ready" && !queryActive && renderable?.truncated ? (
        <p className="border-border bg-card text-muted-foreground pointer-events-none absolute top-3 left-3 z-20 max-w-[min(520px,70%)] rounded-full border px-3 py-1.5 text-xs shadow-soft">
          {t("graph.overviewNotice")}
          <span className="ml-2 tabular-nums">
            {`${t("graph.entityCount")} +${renderable.hiddenEntityCount} · ${t("graph.edgeCount")} +${renderable.hiddenEdgeCount}`}
          </span>
        </p>
      ) : null}
      {state.status === "loading" ? <GraphState tone="loading" /> : null}
      {state.status === "error" ? (
        <GraphState tone="error" detail={state.detail} onRetry={handleRetry} />
      ) : null}
      {isLibraryEmpty ? <GraphState tone="empty" /> : null}
      {isNoMatch ? (
        <GraphState message={t("graph.noMatchHint")} title={t("graph.noMatchTitle")} tone="empty" />
      ) : null}
      {state.status === "ready" && snapshot && summary && layout && !isLibraryEmpty && !isNoMatch ? (
        <GraphCanvas
          detailOverlay={
            selectedEntity ? (
              <EntityCard
                entity={selectedEntity}
                neighbors={neighbors}
                onClose={() => setSelectedId(null)}
                onSelectNeighbor={handleSelectAndLocate}
                typeColors={typeColors}
              />
            ) : null
          }
          focus={focusRequest}
          layout={layout}
          selectedId={activeSelectedId}
          snapshot={snapshot}
          summary={summary}
          onSelect={setSelectedId}
        />
      ) : null}
    </div>
  );
}
