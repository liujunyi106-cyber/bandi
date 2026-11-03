/**
  * 功能：图谱视图变换层（纯函数，）。
  * 技法对齐datafoundry `TraceDagCanvas`：`<g transform="translate scale">` + 光标锚定缩放 + 自适应铺满。
 * 与渲染解耦——本模块只算 transform，SVG/Canvas 都能消费。
 */

export interface ViewTransform {
  x: number;
  y: number;
  scale: number;
}

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export const MIN_SCALE = 0.3;
export const MAX_SCALE = 2.4;

export const IDENTITY_VIEW: ViewTransform = { x: 0, y: 0, scale: 1 };

/** 功能：把缩放系数夹进可交互区间。 */
export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return MIN_SCALE;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

/** 功能：图坐标 → 屏幕坐标。 */
export function graphToScreen(
  view: ViewTransform,
  graphX: number,
  graphY: number,
): { x: number; y: number } {
  return { x: graphX * view.scale + view.x, y: graphY * view.scale + view.y };
}

/** 功能：屏幕坐标 → 图坐标（`graphToScreen` 的逆）。 */
export function screenToGraph(
  view: ViewTransform,
  screenX: number,
  screenY: number,
): { x: number; y: number } {
  return { x: (screenX - view.x) / view.scale, y: (screenY - view.y) / view.scale };
}

/**
 * 功能：以屏幕点 (pointX, pointY) 为锚缩放——**锚点下的图坐标保持不动**（缩放不漂移的核心不变式）。
 * 已在上/下限时直接返回原视图，避免浮点反复换算把视图磨偏。
 */
export function zoomAt(
  view: ViewTransform,
  factor: number,
  pointX: number,
  pointY: number,
): ViewTransform {
  const nextScale = clampScale(view.scale * factor);
  if (nextScale === view.scale) return view;

  const anchor = screenToGraph(view, pointX, pointY);
  return {
    scale: nextScale,
    x: pointX - anchor.x * nextScale,
    y: pointY - anchor.y * nextScale,
  };
}

/** DOMRect 的最小可用形状（便于纯函数单测）。 */
export interface RectLike {
  width: number;
  height: number;
  left: number;
  top: number;
}

/**
 * 功能：客户端坐标 → 画布（viewBox）坐标。
 * SVG 用 `h-full w-full` + 默认 `preserveAspectRatio="xMidYMid meet"` → 内容按 `min(w/W, h/H)`
 * 等比缩放后**居中**，容器多出的部分是留白。必须抵消该留白，否则滚轮缩放的锚点会偏离光标
 * （容器与 viewBox 宽高比不同时偏差可达上百个画布单位）。
 */
export function toCanvasPoint(
  rect: RectLike,
  canvasWidth: number,
  canvasHeight: number,
  clientX: number,
  clientY: number,
): { x: number; y: number } {
  if (canvasWidth <= 0 || canvasHeight <= 0) return { x: 0, y: 0 };

  const scale = Math.min(rect.width / canvasWidth, rect.height / canvasHeight);
  if (!Number.isFinite(scale) || scale <= 0) {
    return { x: canvasWidth / 2, y: canvasHeight / 2 };
  }

  const offsetX = (rect.width - canvasWidth * scale) / 2;
  const offsetY = (rect.height - canvasHeight * scale) / 2;

  return {
    x: (clientX - rect.left - offsetX) / scale,
    y: (clientY - rect.top - offsetY) / scale,
  };
}

/**
 * 功能：把内容边界适配进画布——等比缩放 + 居中，并让内容完整落在 padding 内。
 * `maxScale` 用于初始视图：小图不给顶满上限（否则少节点图初始过大）。
 */
export function fitToBounds(
  bounds: Bounds | null,
  canvasWidth: number,
  canvasHeight: number,
  padding = 48,
  maxScale = MAX_SCALE,
): ViewTransform {
  if (!bounds) return { ...IDENTITY_VIEW };

  const { minX, minY, maxX, maxY } = bounds;
  if (![minX, minY, maxX, maxY, canvasWidth, canvasHeight].every(Number.isFinite)) {
    return { ...IDENTITY_VIEW };
  }
  if (canvasWidth <= 0 || canvasHeight <= 0) return { ...IDENTITY_VIEW };

  const contentWidth = Math.max(0, maxX - minX);
  const contentHeight = Math.max(0, maxY - minY);
  const usableWidth = Math.max(1, canvasWidth - padding * 2);
  const usableHeight = Math.max(1, canvasHeight - padding * 2);

  // 内容为零尺寸时退化为上限缩放（居中一点），不产生 NaN/Infinity
  const scale = clampScale(
    Math.min(
      maxScale,
      contentWidth > 0 ? usableWidth / contentWidth : maxScale,
      contentHeight > 0 ? usableHeight / contentHeight : maxScale,
    ),
  );

  const centerX = (minX + maxX) / 2;
  const centerY = (minY + maxY) / 2;

  return {
    scale,
    x: canvasWidth / 2 - centerX * scale,
    y: canvasHeight / 2 - centerY * scale,
  };
}
