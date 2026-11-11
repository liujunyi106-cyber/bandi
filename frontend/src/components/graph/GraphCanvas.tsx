/**
 * 功能：知识图谱画布（@base docs/前端设计规划.md §6「知识 · 图谱」+ §8 交互规范）。
 * 固定 viewBox + `<g translate/scale>`；光标锚定滚轮缩放、指针捕获拖拽平移、
 * 命中圆 + 键盘可达、类型图例。缩放平移只走滚轮与拖拽（无 +/- 按钮）。
 * 坐标由 `@/lib/graph-layout` 预计算 → 静态渲染，无 rAF 循环（天然满足 reduced-motion）。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";

import {
  buildNodeRadiusMap,
  buildTypeColorMap,
  entityTypeLabelKey,
  truncateLabel,
  typeColorOf,
} from "@/components/graph/graph-visual";
import { useT } from "@/i18n";
import {
  type RectLike,
  type ViewTransform,
  fitToBounds,
  screenToGraph,
  toCanvasPoint,
  zoomAt,
} from "@/lib/graph-view";
import {
  DRAG_TICKS_PER_FRAME,
  MIN_HIT_RADIUS,
  SETTLE_TICKS,
  createGraphSimulation,
  type GraphSimulation,
  type LayoutResult,
} from "@/lib/graph-layout";
import { cn } from "@/lib/utils";
import type { GraphEntity, GraphSnapshot, GraphSummary } from "@/services/graph";

/** 实体数超过此值默认隐藏标签（防文字糊成一片）。 */
const LABEL_ENTITY_LIMIT = 48;
/** 缩放到此倍率以上强制显示标签（用户主动放大了就是要看清）。 */
const LABEL_SCALE_THRESHOLD = 0.85;
/** 视为「点击」而非「拖拽」的位移阈值（px）。 */
const CLICK_SLOP = 4;
/** 实时重排的帧间隔下限（≈30fps）——拖拽期间限制重渲频率，守住大图流畅度。 */
const LIVE_FRAME_INTERVAL_MS = 33;
/** click 抑制窗口（ms）：只有紧随 pointerup 的合成 click 才算拖拽收尾。 */
const SUPPRESS_CLICK_WINDOW_MS = 400;
/** 初始视图留白（内容更居中收拢，不顶到画布边）。 */
const INITIAL_FIT_PADDING = 64;
/** 初始缩放上限：少节点图不放大铺满（否则初始过大）。 */
const INITIAL_FIT_MAX_SCALE = 1.2;

export interface GraphCanvasProps {
  snapshot: GraphSnapshot;
  layout: LayoutResult;
  summary: GraphSummary;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  /** 定位请求（seq 递增触发）：把指定实体平移到画布中心（保持当前缩放）。实体卡 chips 用。 */
  focus?: { id: string; seq: number } | null;
  /** 画布内浮动详情浮层（固定右上角出现，不挤占画布布局）。 */
  detailOverlay?: ReactNode;
}

interface DragState {
  pointerId: number;
  startX: number;
  startY: number;
  /** 起始点的 viewBox 投影——平移增量必须在此空间计算（屏幕像素 ≠ viewBox 单位）。 */
  startCanvasX: number;
  startCanvasY: number;
  /** 拖拽期缓存的容器 rect（避免 move 中反复 getBoundingClientRect 触发布局重算）。 */
  rect: RectLike;
  view: ViewTransform;
  moved: boolean;
}

interface RenderedEdge {
  key: string;
  source: string;
  target: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export function GraphCanvas({
  snapshot,
  layout,
  summary,
  selectedId,
  onSelect,
  focus,
  detailOverlay,
}: GraphCanvasProps) {
  const t = useT();
  const dragRef = useRef<DragState | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const { width, height, bounds, positions, nodeRadius } = layout;
  // 类型 → 灰阶映射（按实体首次出现顺序；画布/图例/实体卡同源）
  const typeColors = useMemo(
    () => buildTypeColorMap(snapshot.entities.map((entity) => entity.type)),
    [snapshot.entities],
  );
  // 实体 → 半径（按连接度分 5 档；基准半径来自布局的密度自适应结果）
  const radiusById = useMemo(
    () => buildNodeRadiusMap(snapshot.entities, snapshot.edges, nodeRadius),
    [snapshot, nodeRadius],
  );

  const [view, setView] = useState<ViewTransform>(() =>
    fitToBounds(bounds, width, height, INITIAL_FIT_PADDING, INITIAL_FIT_MAX_SCALE),
  );

  // 拖拽实时重排：仿真实例 + rAF 句柄 + 实时坐标（null = 用静态布局坐标）
  const simulationRef = useRef<GraphSimulation | null>(null);
  const frameRef = useRef<number | null>(null);
  const lastFrameAtRef = useRef(0);
  const nodeDragRef = useRef<{
    pointerId: number;
    id: string;
    moved: boolean;
    rect: RectLike;
    startX: number;
    startY: number;
  } | null>(null);
  /** 拖拽收尾抑制的**时刻**（时间窗内才吞 click，防残留标志吞掉后续真实点击）。 */
  const suppressNodeClickAtRef = useRef(0);
  const reduceMotionRef = useRef(false);
  const [livePositions, setLivePositions] = useState<Map<string, { x: number; y: number }> | null>(
    null,
  );
  const activePositions = livePositions ?? positions;

  // 布局变化（首次/换图）自动适配；选择变化不重置视图，避免用户手动调好的视角被抢走
  useEffect(() => {
    setView(fitToBounds(bounds, width, height, INITIAL_FIT_PADDING, INITIAL_FIT_MAX_SCALE));
  }, [bounds, width, height]);

  // 实时坐标的最新值（focus 定位读取；拖拽后静态 layout.positions 已不是渲染真相）
  const activePositionsRef = useRef(activePositions);
  activePositionsRef.current = activePositions;

  // 定位请求：把目标实体平移到画布中心（保持用户当前缩放；目标不在图里则忽略）。
  // 依赖仅 focus 的 id/seq 与尺寸——layout 对象在搜索过滤后会重建，不应把视图拉回旧选中。
  const focusId = focus?.id;
  const focusSeq = focus?.seq;
  useEffect(() => {
    if (!focusId) return;
    const point = activePositionsRef.current.get(focusId);
    if (!point) return;
    setView((current) => ({
      scale: current.scale,
      x: width / 2 - point.x * current.scale,
      y: height / 2 - point.y * current.scale,
    }));
  }, [focusId, focusSeq, height, width]);

  /** 换图/卸载：停 rAF、释放仿真、回到静态坐标（否则会残留上一张图的实时坐标） */
  useEffect(() => {
    setLivePositions(null);
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
      simulationRef.current?.dispose();
      simulationRef.current = null;
    };
  }, [snapshot]);

  // finesse §7 强制：动效需有 reduced-motion 终态。此处直接在松手时一次性落定，不跑逐帧动画。
  useEffect(() => {
    // 老环境/精简 WebView 可能没有 matchMedia：退化为「不检测」，绝不让画布挂掉
    if (typeof window.matchMedia !== "function") return;

    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => {
      reduceMotionRef.current = query.matches;
    };
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  // React 18 把 wheel 在根容器上注册为 **passive**（react-dom 手动模拟浏览器默认行为
  // 见 `react-dom.development.js` addTrappedEventListener 的 isPassiveListener 分支）
  // 因此 onWheel 里 preventDefault() 是空操作 → 缩放时页面会跟着滚。必须自行注册 non-passive。
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const onWheel = (event: WheelEvent) => {
      // 浮层（实体详情卡）内的滚轮是列表滚动，不是画布缩放——放行默认行为
      if ((event.target as Element).closest?.("[data-graph-overlay]")) return;
      event.preventDefault();
      const point = toCanvasPoint(
        container.getBoundingClientRect(),
        width,
        height,
        event.clientX,
        event.clientY,
      );
      setView((current) => zoomAt(current, event.deltaY > 0 ? 0.9 : 1.1, point.x, point.y));
    };
    container.addEventListener("wheel", onWheel, { passive: false });
    return () => container.removeEventListener("wheel", onWheel);
  }, [height, width]);

  /** 功能：推进实时重排帧（帧间隔下限节流，守住大图流畅度）；冷却后自动停 rAF。 */
  const runFrames = useCallback(() => {
    if (frameRef.current !== null) return;
    const step = (now: number) => {
      const simulation = simulationRef.current;
      if (!simulation) {
        frameRef.current = null;
        return;
      }
      if (now - lastFrameAtRef.current < LIVE_FRAME_INTERVAL_MS) {
        frameRef.current = requestAnimationFrame(step);
        return;
      }
      lastFrameAtRef.current = now;
      const active = simulation.advance(DRAG_TICKS_PER_FRAME);
      setLivePositions(new Map(simulation.positions));
      frameRef.current = active ? requestAnimationFrame(step) : null;
    };
    frameRef.current = requestAnimationFrame(step);
  }, []);

  /** 功能：按需创建实时仿真；概览模式（超规模）返回 null → 该规模退化为仅可点选。 */
  const ensureSimulation = useCallback(() => {
    if (simulationRef.current) return simulationRef.current;
    simulationRef.current = createGraphSimulation(snapshot, {
      width,
      height,
      nodeRadius,
      initialPositions: positions,
    });
    return simulationRef.current;
  }, [height, nodeRadius, positions, snapshot, width]);

  /** 功能：节点按下 → 钉住并升温，进入拖拽重排（reduced-motion 下不做逐帧动画）。 */
  const handleNodePointerDown = useCallback(
    (event: ReactPointerEvent<SVGGElement>, id: string) => {
      if (event.button !== 0) return;
      const container = containerRef.current;
      if (!container) return;
      // 概览模式不建仿真（返回 null）→ 节点退化为仅可点选
      if (!ensureSimulation()) return;

      nodeDragRef.current = {
        pointerId: event.pointerId,
        id,
        moved: false,
        rect: container.getBoundingClientRect(),
        startX: event.clientX,
        startY: event.clientY,
      };
      // capture 必须设在节点 <g>（event.currentTarget）而非容器：capture 会把后续
      // pointer 事件重定向，而 pointer 生成的 click 以「down/up 目标的最近公共祖先」
      // 为目标 —— 设在容器上会让 click 落到容器，节点 onClick 永远不触发（真浏览器）。
      // 设在 <g> 上则 down/up 都指向 <g>，click 正常派发，move/up 冒泡到容器不受影响。
      event.currentTarget.setPointerCapture?.(event.pointerId);
      // 按下不升温、不启动帧循环：纯点击选中不应触发全图重排；拖动证据出现在
      // pointermove 超过 CLICK_SLOP 之后才钉住+升温（-3 修复）
    },
    [ensureSimulation],
  );

  const adjacency = useMemo(() => {
    const map = new Map<string, Set<string>>();
    for (const entity of snapshot.entities) map.set(entity.id, new Set());
    for (const edge of snapshot.edges) {
      map.get(edge.source)?.add(edge.target);
      map.get(edge.target)?.add(edge.source);
    }
    return map;
  }, [snapshot]);

  const neighborIds = useMemo(
    () => (selectedId ? (adjacency.get(selectedId) ?? new Set<string>()) : null),
    [adjacency, selectedId],
  );

  const renderedEdges = useMemo<RenderedEdge[]>(() => {
    const edges: RenderedEdge[] = [];
    snapshot.edges.forEach((edge, index) => {
      const from = activePositions.get(edge.source);
      const to = activePositions.get(edge.target);
      if (!from || !to) return;
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const length = Math.hypot(dx, dy) || 1;
      // 两端按各自半径裁边（节点有 5 档大小梯度，裁剪量随档位走）
      const trimFrom = (radiusById.get(edge.source) ?? nodeRadius) + 3;
      const trimTo = (radiusById.get(edge.target) ?? nodeRadius) + 3;
      edges.push({
        key: `${edge.source}\u0000${edge.target}\u0000${index}`,
        source: edge.source,
        target: edge.target,
        x1: from.x + (dx / length) * trimFrom,
        y1: from.y + (dy / length) * trimFrom,
        x2: to.x - (dx / length) * trimTo,
        y2: to.y - (dy / length) * trimTo,
      });
    });
    return edges;
  }, [snapshot, activePositions, nodeRadius, radiusById]);

  const showLabels =
    snapshot.entities.length <= LABEL_ENTITY_LIMIT || view.scale >= LABEL_SCALE_THRESHOLD;

  // 元素记忆化：`view` 只挂在父 <g> 的 transform 上，拖动/缩放时子元素引用不变 → React 跳过重渲
  // （否则每次 pointermove 都要重渲全部节点，800 节点必然掉帧）。
  const edgeElements = useMemo(
    () =>
      renderedEdges.map((edge) => {
        const active =
          selectedId !== null && (edge.source === selectedId || edge.target === selectedId);
        const dimmed = selectedId !== null && !active;
        return (
          <line
            key={edge.key}
            x1={edge.x1}
            y1={edge.y1}
            x2={edge.x2}
            y2={edge.y2}
            // 选中边走中灰（不抢节点视觉），线宽只比常态略粗
            stroke={active ? "var(--graph-gray-3)" : "var(--border-strong)"}
            strokeWidth={active ? 1.4 : 1}
            strokeOpacity={dimmed ? 0.18 : active ? 0.85 : 0.45}
          />
        );
      }),
    [renderedEdges, selectedId],
  );

  const nodeElements = useMemo(
    () =>
      snapshot.entities.map((entity) => {
        const point = activePositions.get(entity.id);
        if (!point) return null;
        const isSelected = entity.id === selectedId;
        const isNeighbor = neighborIds?.has(entity.id) ?? false;
        return (
          <GraphNodeGlyph
            key={entity.id}
            entity={entity}
            x={point.x}
            y={point.y}
            radius={radiusById.get(entity.id) ?? nodeRadius}
            selected={isSelected}
            dimmed={selectedId !== null && !isSelected && !isNeighbor}
            showLabel={showLabels}
            typeColors={typeColors}
            onSelect={() => {
              // 节点拖拽结束时 pointerup 目标是 <g>（capture 在其上），会同步派发一次
              // click —— 那是拖拽的收尾而非选中意图，用**时间窗**抑制（若那次合成
              // click 未派发，超窗后的真实点击必须正常生效，不能被残留标志吞掉）。
              if (performance.now() - suppressNodeClickAtRef.current < SUPPRESS_CLICK_WINDOW_MS) {
                suppressNodeClickAtRef.current = 0;
                return;
              }
              onSelect(entity.id);
            }}
            onPointerDownNode={handleNodePointerDown}
          />
        );
      }),
    [
      activePositions,
      handleNodePointerDown,
      neighborIds,
      nodeRadius,
      onSelect,
      radiusById,
      selectedId,
      showLabels,
      snapshot.entities,
      typeColors,
    ],
  );

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if ((event.target as Element).closest?.("[data-graph-node]")) return;
    const container = containerRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const start = toCanvasPoint(rect, width, height, event.clientX, event.clientY);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startCanvasX: start.x,
      startCanvasY: start.y,
      rect,
      view,
      moved: false,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const nodeDrag = nodeDragRef.current;
    if (nodeDrag && nodeDrag.pointerId === event.pointerId) {
      const container = containerRef.current;
      const simulation = simulationRef.current;
      if (!container || !simulation) return;
      if (!nodeDrag.moved) {
        // 位移超过阈值才算拖动（与平移同口径）：1px 抖动不进入拖拽、不吞点选
        const dx = event.clientX - nodeDrag.startX;
        const dy = event.clientY - nodeDrag.startY;
        if (Math.hypot(dx, dy) <= CLICK_SLOP) return;
        nodeDrag.moved = true;
      }
      // 屏幕 → viewBox（抵消 letterbox/显示缩放）→ 图坐标（视图反变换）：
      // d3 坐标系是 <g> 内部空间，缺最后一步会让拖拽在缩放/平移后整体跑偏。
      const canvasPoint = toCanvasPoint(nodeDrag.rect, width, height, event.clientX, event.clientY);
      const graphPoint = screenToGraph(view, canvasPoint.x, canvasPoint.y);
      simulation.pin(nodeDrag.id, graphPoint.x, graphPoint.y);
      // 渲染由 rAF 帧循环统一 setLivePositions（每 pointermove 写状态会超出帧率，-3 修复）；
      // reduced-motion 无帧循环 → 同步落一次保证节点跟手（仅去动画，不去反馈）
      if (reduceMotionRef.current) setLivePositions(new Map(simulation.positions));
      else runFrames();
      return;
    }

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const offsetX = event.clientX - drag.startX;
    const offsetY = event.clientY - drag.startY;
    if (Math.hypot(offsetX, offsetY) > CLICK_SLOP) drag.moved = true;
    // 平移增量在 viewBox 空间计算——直接加屏幕像素会在容器与 viewBox 比例不同时漂移
    const now = toCanvasPoint(drag.rect, width, height, event.clientX, event.clientY);
    setView({
      ...drag.view,
      x: drag.view.x + (now.x - drag.startCanvasX),
      y: drag.view.y + (now.y - drag.startCanvasY),
    });
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const nodeDrag = nodeDragRef.current;
    if (nodeDrag && nodeDrag.pointerId === event.pointerId) {
      nodeDragRef.current = null;
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      const simulation = simulationRef.current;
      // 纯点击（未拖动）零副作用：未钉住、未升温、未启动帧循环，直接结束
      if (simulation && nodeDrag.moved) {
        simulation.unpin(nodeDrag.id);
        suppressNodeClickAtRef.current = performance.now(); // 拖拽结束不当作「选中」
        if (reduceMotionRef.current) {
          // reduced-motion 终态：一次性落定，不跑逐帧动画
          simulation.advance(SETTLE_TICKS);
          setLivePositions(new Map(simulation.positions));
        } else {
          runFrames();
        }
      }
      return;
    }

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    // 空白处「点击」（非拖拽）清空选中
    if (!drag.moved && !(event.target as Element).closest?.("[data-graph-node]")) onSelect(null);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") onSelect(null);
  };

  return (
    // 画布：白底无描边、无外框（缩放=滚轮、平移=按住拖拽、缩放平移无按钮）；
    // 统计胶囊为画布内左上浮层，不占布局行
    <div
      ref={containerRef}
      className="bg-card relative h-full min-h-0 flex-1 touch-none overflow-hidden"
      onKeyDown={handleKeyDown}
      onPointerCancel={handlePointerUp}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerUp}
      tabIndex={-1}
    >
      <svg
        aria-label={t("graph.canvasLabel")}
        className="h-full w-full"
        role="group"
        viewBox={`0 0 ${width} ${height}`}
      >
        <g transform={`translate(${view.x} ${view.y}) scale(${view.scale})`}>
          {edgeElements}
          {nodeElements}
        </g>
      </svg>

      <div className="pointer-events-none absolute top-3 left-3 z-10 flex flex-wrap items-center gap-2">
        <GraphStat label={t("graph.entityCount")} value={summary.entityCount} />
        <GraphStat label={t("graph.edgeCount")} value={summary.edgeCount} />
        {summary.orphanEntityIds.length > 0 ? (
          <GraphStat label={t("graph.orphanCount")} value={summary.orphanEntityIds.length} muted />
        ) : null}
      </div>

      {detailOverlay ? (
        // 浮层是画布容器的子元素：拦截 pointerdown 传播，避免容器把浮层内的
        // 点击当作平移起点（pointerup 清空选中会误关详情卡）
        <div
          className="absolute top-3 right-3 z-10 flex max-h-[calc(100%-1.5rem)] w-72 min-h-0 flex-col"
          data-graph-overlay=""
          onPointerDown={(event) => event.stopPropagation()}
        >
          {detailOverlay}
        </div>
      ) : null}
      <GraphLegend summary={summary} typeColors={typeColors} />

      <p className="text-muted-foreground-weak pointer-events-none absolute right-3 bottom-3 text-[11px]">
        {t("graph.selectHint")}
      </p>
    </div>
  );
}

/** 功能：顶部统计小胶囊。 */
function GraphStat({
  label,
  value,
  muted = false,
}: {
  label: string;
  value: number;
  muted?: boolean;
}) {
  return (
    <span
      className={cn(
        "border-border inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs",
        muted ? "text-muted-foreground" : "text-foreground",
      )}
    >
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">{value}</span>
    </span>
  );
}

interface GraphNodeGlyphProps {
  entity: GraphEntity;
  x: number;
  y: number;
  /** 可视半径（连接度梯度档位 × 密度自适应基准）。 */
  radius: number;
  selected: boolean;
  dimmed: boolean;
  showLabel: boolean;
  onSelect: () => void;
  /** 按下即进入拖拽重排（同时订阅容器接管指针，避免与平移冲突）。 */
  onPointerDownNode: (event: ReactPointerEvent<SVGGElement>, id: string) => void;
  /** 类型 → 颜色映射（与图例/缩略图/实体卡同源）。 */
  typeColors: ReadonlyMap<string, string>;
}

/** 功能：单个实体节点（实心圆 + 标签；选中只降邻域灰并由标签加重表达，无描边/光晕）。 */
function GraphNodeGlyph({
  entity,
  x,
  y,
  radius,
  selected,
  dimmed,
  showLabel,
  onSelect,
  onPointerDownNode,
  typeColors,
}: GraphNodeGlyphProps) {
  const t = useT();
  const color = typeColorOf(typeColors, entity.type);
  const typeLabel = t(entityTypeLabelKey(entity.type));
  // 命中区与渲染半径解耦：节点缩到 3px 时也不能变成难点的小目标
  const hitRadius = Math.max(MIN_HIT_RADIUS, radius + 8);

  const handleKeyDown = (event: ReactKeyboardEvent<SVGGElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect();
    }
  };

  return (
    <g
      data-graph-node={entity.id}
      role="button"
      tabIndex={0}
      aria-label={`${entity.name}（${typeLabel}）`}
      transform={`translate(${x} ${y})`}
      className="cursor-pointer outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
      opacity={dimmed ? 0.28 : 1}
      onClick={(event) => {
        event.stopPropagation();
        onSelect();
      }}
      onKeyDown={handleKeyDown}
      onPointerDown={(event) => {
        event.stopPropagation();
        onPointerDownNode(event, entity.id);
      }}
    >
      <title>{`${entity.name}（${typeLabel}）`}</title>
      <circle r={hitRadius} fill="transparent" />
      <circle r={radius} fill={color} />
      {showLabel ? (
        <text
          y={radius + 14}
          textAnchor="middle"
          className={cn(
            "pointer-events-none text-[10px]",
            selected ? "fill-foreground font-semibold" : "fill-muted-foreground font-medium",
          )}
        >
          {truncateLabel(entity.name, 10)}
        </text>
      ) : null}
    </g>
  );
}

/** 功能：类型图例（含各类计数，灰阶与节点填充同源——同一 typeColors 映射）。 */
function GraphLegend({
  summary,
  typeColors,
}: {
  summary: GraphSummary;
  typeColors: ReadonlyMap<string, string>;
}) {
  const t = useT();
  const visible = (Object.entries(summary.typeCounts) as [GraphEntity["type"], number][]).filter(
    ([, count]) => count > 0,
  );
  if (visible.length === 0) return null;

  return (
    <div className="border-border bg-card/90 pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-2 rounded-full border px-3 py-1.5 shadow-soft">
      {visible.map(([type, count]) => (
        <span
          key={type}
          className="text-muted-foreground inline-flex items-center gap-1 text-[11px]"
        >
          <span
            className="size-2 rounded-full"
            style={{ backgroundColor: typeColorOf(typeColors, type) }}
            aria-hidden="true"
          />
          {t(entityTypeLabelKey(type))}
          <span className="tabular-nums">{count}</span>
        </span>
      ))}
    </div>
  );
}
