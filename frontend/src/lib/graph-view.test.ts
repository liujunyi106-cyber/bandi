import { describe, expect, it } from "vitest";

import {
  MAX_SCALE,
  MIN_SCALE,
  clampScale,
  fitToBounds,
  graphToScreen,
  screenToGraph,
  toCanvasPoint,
  zoomAt,
  type Bounds,
  type ViewTransform,
} from "@/lib/graph-view";

const IDENTITY: ViewTransform = { x: 0, y: 0, scale: 1 };

describe("zoomAt", () => {
  it("保持光标下的图坐标不动（视图变换的核心不变式）", () => {
    const view: ViewTransform = { x: 40, y: -20, scale: 1.4 };
    const pointX = 300;
    const pointY = 220;
    const before = screenToGraph(view, pointX, pointY);

    const zoomed = zoomAt(view, 1.25, pointX, pointY);
    const after = screenToGraph(zoomed, pointX, pointY);

    expect(after.x).toBeCloseTo(before.x, 6);
    expect(after.y).toBeCloseTo(before.y, 6);
    expect(zoomed.scale).toBeCloseTo(1.75, 6);
  });

  it("缩放到上下限即止，不越界", () => {
    const zoomedOut = zoomAt(IDENTITY, 0.01, 100, 100);
    expect(zoomedOut.scale).toBe(MIN_SCALE);

    const zoomedIn = zoomAt(IDENTITY, 100, 100, 100);
    expect(zoomedIn.scale).toBe(MAX_SCALE);
  });

  it("已达上限后再放大，视图不漂移", () => {
    const atMax: ViewTransform = { x: 10, y: 10, scale: MAX_SCALE };
    expect(zoomAt(atMax, 1.5, 250, 180)).toEqual(atMax);
  });
});

describe("graphToScreen / screenToGraph", () => {
  it("互为逆变换", () => {
    const view: ViewTransform = { x: -35, y: 72, scale: 0.8 };
    const graphPoint = { x: 128.5, y: -64.25 };
    const screen = graphToScreen(view, graphPoint.x, graphPoint.y);
    const roundTrip = screenToGraph(view, screen.x, screen.y);

    expect(roundTrip.x).toBeCloseTo(graphPoint.x, 6);
    expect(roundTrip.y).toBeCloseTo(graphPoint.y, 6);
  });
});

describe("clampScale", () => {
  it("夹在 MIN_SCALE 与 MAX_SCALE 之间", () => {
    expect(clampScale(0)).toBe(MIN_SCALE);
    expect(clampScale(1)).toBe(1);
    expect(clampScale(99)).toBe(MAX_SCALE);
  });
});

describe("fitToBounds", () => {
  const bounds: Bounds = { minX: -100, minY: -50, maxX: 300, maxY: 250 };

  it("适配后内容居中且不超出画布（含 padding）", () => {
    const canvasWidth = 920;
    const canvasHeight = 620;
    const padding = 48;
    const view = fitToBounds(bounds, canvasWidth, canvasHeight, padding);

    const topLeft = graphToScreen(view, bounds.minX, bounds.minY);
    const bottomRight = graphToScreen(view, bounds.maxX, bounds.maxY);

    expect(topLeft.x).toBeGreaterThanOrEqual(padding - 0.001);
    expect(topLeft.y).toBeGreaterThanOrEqual(padding - 0.001);
    expect(bottomRight.x).toBeLessThanOrEqual(canvasWidth - padding + 0.001);
    expect(bottomRight.y).toBeLessThanOrEqual(canvasHeight - padding + 0.001);

    const centerX = (topLeft.x + bottomRight.x) / 2;
    const centerY = (topLeft.y + bottomRight.y) / 2;
    expect(centerX).toBeCloseTo(canvasWidth / 2, 4);
    expect(centerY).toBeCloseTo(canvasHeight / 2, 4);
  });

  it("内容极小时放大被 MAX_SCALE 封顶（不无限放大）", () => {
    const tiny: Bounds = { minX: 0, minY: 0, maxX: 1, maxY: 1 };
    expect(fitToBounds(tiny, 920, 620, 48).scale).toBe(MAX_SCALE);
  });

  it("maxScale 参数生效：初始视图用更低的缩放上限（小图不铺满）", () => {
    const tiny: Bounds = { minX: 0, minY: 0, maxX: 1, maxY: 1 };
    expect(fitToBounds(tiny, 920, 620, 64, 1.2).scale).toBe(1.2);
  });

  it("maxScale 不干扰正常适配：大内容仍按可用空间算缩放", () => {
    const view = fitToBounds(bounds, 920, 620, 48, 1.2);
    const topLeft = graphToScreen(view, bounds.minX, bounds.minY);
    const bottomRight = graphToScreen(view, bounds.maxX, bounds.maxY);
    expect(topLeft.x).toBeGreaterThanOrEqual(48 - 0.001);
    expect(bottomRight.x).toBeLessThanOrEqual(920 - 48 + 0.001);
  });

  it("零尺寸 / 非法 bounds 退化为合法变换（不产生 NaN）", () => {
    const degenerate: Bounds = { minX: 5, minY: 5, maxX: 5, maxY: 5 };
    const view = fitToBounds(degenerate, 920, 620, 48);
    expect(Number.isFinite(view.x)).toBe(true);
    expect(Number.isFinite(view.y)).toBe(true);
    expect(view.scale).toBeGreaterThan(0);

    const empty = fitToBounds(null, 920, 620, 48);
    expect(empty).toEqual(IDENTITY);
  });
});

describe("toCanvasPoint（SVG preserveAspectRatio=meet 的 letterbox 校正）", () => {
  const CANVAS_WIDTH = 920;
  const CANVAS_HEIGHT = 620;

  function rect(width: number, height: number, left = 0, top = 0) {
    return { width, height, left, top };
  }

  it("容器与 viewBox 等比：按比例线性映射，中心点落在画布中心", () => {
    const point = toCanvasPoint(
      rect(460, 310, 10, 20),
      CANVAS_WIDTH,
      CANVAS_HEIGHT,
      10 + 230,
      20 + 155,
    );
    expect(point.x).toBeCloseTo(460, 6);
    expect(point.y).toBeCloseTo(310, 6);
  });

  it("容器更宽（左右留白）：左边缘映射为负 x，中心仍是画布中心", () => {
    // 1840×620 容器：scale=1，左右各留 460px letterbox
    const container = rect(1840, CANVAS_HEIGHT, 100, 0);

    const atCenter = toCanvasPoint(container, CANVAS_WIDTH, CANVAS_HEIGHT, 100 + 920, 310);
    expect(atCenter.x).toBeCloseTo(460, 6);
    expect(atCenter.y).toBeCloseTo(310, 6);

    const atLeftEdge = toCanvasPoint(container, CANVAS_WIDTH, CANVAS_HEIGHT, 100, 0);
    expect(atLeftEdge.x).toBeCloseTo(-460, 6);
    expect(atLeftEdge.y).toBeCloseTo(0, 6);
  });

  it("容器更高（上下留白）：顶边缘映射为负 y", () => {
    // 920×1240 容器：scale=1，上下各留 310px
    const container = rect(920, 1240, 0, 50);
    const atTopEdge = toCanvasPoint(container, CANVAS_WIDTH, CANVAS_HEIGHT, 0, 50);
    expect(atTopEdge.x).toBeCloseTo(0, 6);
    expect(atTopEdge.y).toBeCloseTo(-310, 6);
  });

  it("容器整体缩小（scale<1）：仍按缩放后的实际渲染尺寸映射", () => {
    // 460×310 容器 → scale=0.5，无 letterbox；右下角 = 画布右下角
    const container = rect(460, 310, 0, 0);
    const bottomRight = toCanvasPoint(container, CANVAS_WIDTH, CANVAS_HEIGHT, 460, 310);
    expect(bottomRight.x).toBeCloseTo(CANVAS_WIDTH, 6);
    expect(bottomRight.y).toBeCloseTo(CANVAS_HEIGHT, 6);
  });

  it("零尺寸容器退化为画布中心（不产生 NaN/Infinity）", () => {
    const point = toCanvasPoint(rect(0, 0), CANVAS_WIDTH, CANVAS_HEIGHT, 5, 5);
    expect(point.x).toBeCloseTo(CANVAS_WIDTH / 2, 6);
    expect(point.y).toBeCloseTo(CANVAS_HEIGHT / 2, 6);
  });
});
