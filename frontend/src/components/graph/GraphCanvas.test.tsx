/**
 * 功能：GraphCanvas 交互坐标契约——三坐标空间不得混用：
 * 屏幕客户端坐标 → `toCanvasPoint` → viewBox 坐标 → `screenToGraph` → 图坐标。
 * - 平移：屏幕位移必须按显示缩放（容器尺寸 vs viewBox 比例）换算为 viewBox 位移；
 * - 节点拖拽：pin 必须落图坐标（viewBox 坐标经视图反变换），缩放/平移后不跑偏；
 * - 拖拽收尾的 click 抑制必须有时间窗（避免一次拖拽后「下一次点击被永久吞掉」）。
 */
import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GraphCanvas } from "@/components/graph/GraphCanvas";
import { layoutGraph } from "@/lib/graph-layout";
import { describeGraph, type GraphSnapshot } from "@/services/graph";

const FIXTURE: GraphSnapshot = {
  entities: [
    { id: "a", type: "project", name: "甲项目" },
    { id: "b", type: "task", name: "乙任务" },
  ],
  edges: [{ source: "b", target: "a", type: "belongs_to" }],
};

/** 容器显示尺寸固定 1200×500（与 viewBox 920×620 比例不同 → 必有显示缩放 + letterbox）。 */
const RECT = { width: 1200, height: 500, left: 0, top: 0 };
const VIEW_W = 920;
const VIEW_H = 620;

function displayScale(): number {
  return Math.min(RECT.width / VIEW_W, RECT.height / VIEW_H);
}

/** 屏幕点 → viewBox 坐标（与生产 toCanvasPoint 同公式，测试侧独立复算）。 */
function canvasPoint(clientX: number, clientY: number): { x: number; y: number } {
  const scale = displayScale();
  const offsetX = (RECT.width - VIEW_W * scale) / 2;
  const offsetY = (RECT.height - VIEW_H * scale) / 2;
  return { x: (clientX - RECT.left - offsetX) / scale, y: (clientY - RECT.top - offsetY) / scale };
}

function readView(): { x: number; y: number; scale: number } {
  const group = document.querySelector("svg g") as SVGGElement;
  const match = /translate\(([-\d.eE+]+)[ ,]+([-\d.eE+]+)\)\s*scale\(([-\d.eE+]+)\)/.exec(
    group.getAttribute("transform") ?? "",
  );
  return { x: Number(match?.[1]), y: Number(match?.[2]), scale: Number(match?.[3]) };
}

function readNode(id: string): { x: number; y: number } {
  const node = document.querySelector(`[data-graph-node="${id}"]`) as SVGGElement;
  const match = /translate\(([-\d.eE+]+)[ ,]+([-\d.eE+]+)\)/.exec(
    node.getAttribute("transform") ?? "",
  );
  return { x: Number(match?.[1]), y: Number(match?.[2]) };
}

function firePointer(
  target: Element,
  type: "pointerdown" | "pointermove" | "pointerup",
  init: { clientX: number; clientY: number; pointerId?: number },
): void {
  // jsdom 26 没有 PointerEvent（fireEvent.pointerX 降级为普通 Event，button/clientX 被丢弃）
  // 而产品代码以 event.button / event.clientX / event.pointerId 分流 —— 用 MouseEvent +
  // defineProperty 构造真浏览器等价的形状。
  const event = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    button: 0,
    clientX: init.clientX,
    clientY: init.clientY,
  });
  Object.defineProperty(event, "pointerId", { value: init.pointerId ?? 1 });
  fireEvent(target, event);
}

describe("GraphCanvas 坐标契约", () => {
  beforeEach(() => {
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
      width: RECT.width,
      height: RECT.height,
      left: RECT.left,
      top: RECT.top,
      right: RECT.left + RECT.width,
      bottom: RECT.top + RECT.height,
      x: RECT.left,
      y: RECT.top,
      toJSON: () => ({}),
    } as DOMRect);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function renderCanvas() {
    const layout = layoutGraph(FIXTURE);
    const summary = describeGraph(FIXTURE);
    const onSelect = vi.fn();
    const { container } = render(
      <GraphCanvas
        layout={layout}
        selectedId={null}
        snapshot={FIXTURE}
        summary={summary}
        onSelect={onSelect}
      />,
    );
    const canvas = container.querySelector("svg")?.parentElement as HTMLElement;
    return { canvas, onSelect };
  }

  it("平移：屏幕位移按显示缩放换算为 viewBox 位移（拖拽跟随光标，不漂移）", () => {
    const { canvas } = renderCanvas();
    const before = readView();

    firePointer(canvas, "pointerdown", { clientX: 500, clientY: 200 });
    firePointer(canvas, "pointermove", { clientX: 600, clientY: 200 });
    firePointer(canvas, "pointerup", { clientX: 600, clientY: 200 });

    const after = readView();
    expect(after.x - before.x).toBeCloseTo(100 / displayScale(), 1);
    expect(after.y - before.y).toBeCloseTo(0, 5);
  });

  it("节点拖拽：pin 落图坐标系（viewBox 经视图反变换，缩放/平移后不跑偏）", async () => {
    const { canvas } = renderCanvas();
    const view = readView();
    const node = canvas.querySelector('[data-graph-node="a"]') as SVGGElement;

    firePointer(node, "pointerdown", { clientX: 300, clientY: 150 });
    firePointer(node, "pointermove", { clientX: 700, clientY: 300 });

    const cp = canvasPoint(700, 300);
    const expected = { x: (cp.x - view.x) / view.scale, y: (cp.y - view.y) / view.scale };
    // -3：拖拽渲染由 rAF 帧循环统一推进（不再每个 pointermove 同步写状态）——
    // 在**钉住期间**（松手前）等帧，断言 pin 精确落图坐标（松手解钉后节点会自然冷却漂移）
    await waitFor(() => {
      const pinned = readNode("a");
      expect(pinned.x).toBeCloseTo(Math.min(VIEW_W, Math.max(0, expected.x)), 0);
      expect(pinned.y).toBeCloseTo(Math.min(VIEW_H, Math.max(0, expected.y)), 0);
    });

    firePointer(node, "pointerup", { clientX: 700, clientY: 300 });
  });

  it("纯点击节点零副作用：不钉住、不升温（节点位置不变），点击选中正常", async () => {
    const { canvas, onSelect } = renderCanvas();
    const node = canvas.querySelector('[data-graph-node="a"]') as SVGGElement;
    const before = readNode("a");

    firePointer(node, "pointerdown", { clientX: 300, clientY: 150 });
    firePointer(node, "pointerup", { clientX: 300, clientY: 150 });
    fireEvent.click(node);

    expect(onSelect).toHaveBeenCalledWith("a");
    // 单击未启动实时重排：等待若干帧后节点仍停在原坐标（无全图缓动的可见结果之一）
    await new Promise((resolve) => setTimeout(resolve, 80));
    const after = readNode("a");
    expect(after.x).toBeCloseTo(before.x, 5);
    expect(after.y).toBeCloseTo(before.y, 5);
  });

  it("拖动阈值：位移 ≤4px 不算拖拽——收尾 click 不被抑制，点选照常", () => {
    const { canvas, onSelect } = renderCanvas();
    const node = canvas.querySelector('[data-graph-node="a"]') as SVGGElement;

    firePointer(node, "pointerdown", { clientX: 300, clientY: 150 });
    firePointer(node, "pointermove", { clientX: 301, clientY: 151 });
    firePointer(node, "pointerup", { clientX: 301, clientY: 151 });
    fireEvent.click(node);

    expect(onSelect).toHaveBeenCalledWith("a");
  });

  it("拖拽收尾的抑制窗口：紧随松手的 click 被吞（不误选中）", () => {
    const { canvas, onSelect } = renderCanvas();
    const node = canvas.querySelector('[data-graph-node="a"]') as SVGGElement;

    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(10_000);
    firePointer(node, "pointerdown", { clientX: 300, clientY: 150 });
    firePointer(node, "pointermove", { clientX: 700, clientY: 300 });
    firePointer(node, "pointerup", { clientX: 700, clientY: 300 });

    fireEvent.click(node);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("拖拽收尾 click 未派发时不残留抑制：超窗后的真实点击正常选中", () => {
    const { canvas, onSelect } = renderCanvas();
    const node = canvas.querySelector('[data-graph-node="a"]') as SVGGElement;

    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(10_000);
    firePointer(node, "pointerdown", { clientX: 300, clientY: 150 });
    firePointer(node, "pointermove", { clientX: 700, clientY: 300 });
    firePointer(node, "pointerup", { clientX: 700, clientY: 300 });
    // 模拟合成 click 未被派发（浏览器差异/中断）——不点

    nowSpy.mockReturnValue(11_000);
    fireEvent.click(node);
    expect(onSelect).toHaveBeenCalledWith("a");
  });
});
