import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import KnowledgePage from "@/pages/knowledge";
import { useKnowledgeStore } from "@/stores/knowledge-store";

const FIXTURE = {
  entities: [
    { id: "p1", type: "project", name: "Bandi Agent" },
    { id: "t1", type: "task", name: "实现图谱" },
    { id: "k1", type: "knowledge", name: "力导向布局" },
    { id: "n1", type: "note", name: "孤立笔记" },
  ],
  edges: [
    { source: "t1", target: "p1", type: "belongs_to" },
    { source: "k1", target: "p1", type: "references" },
  ],
};

/** 功能：把全局 fetch 换成图谱快照桩（覆盖 apiFetch → 归一化 → 布局 → 渲染整链）。 */
function stubGraphFetch(payload: unknown, ok = true, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(payload), {
          status: ok ? status : 500,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
}


/** 功能：渲染知识库并切到图谱页签——页面默认进入文档资料页。 */
async function renderGraphPage() {
  const result = render(<KnowledgePage />);
  await userEvent.click(screen.getByRole("tab", { name: "图谱" }));
  return result;
}

afterEach(() => {
  vi.unstubAllGlobals();
  useKnowledgeStore.getState().resetQuery();
});

describe("KnowledgePage 图谱页", () => {
  it("拉取快照后渲染全部实体节点与类型图例", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    // 图例按类型聚合（项目 1 / 任务 1 / 知识 1 / 笔记 1）
    expect(screen.getByText("项目")).toBeInTheDocument();
    expect(screen.getByText("知识")).toBeInTheDocument();
    expect(screen.getByText("孤立")).toBeInTheDocument();
  });

  it("点选节点后右侧出现实体详情，并列出该实体的关联实体", async () => {
    stubGraphFetch(FIXTURE);
    const user = userEvent.setup();
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    await user.click(container.querySelector('[data-graph-node="p1"]') as Element);

    const detail = await screen.findByRole("complementary");
    expect(within(detail).getByText("Bandi Agent")).toBeInTheDocument();
    // p1 的邻接：t1 + k1（且按名称排序，中文名在前）
    expect(within(detail).getByText("实现图谱")).toBeInTheDocument();
    expect(within(detail).getByText("力导向布局")).toBeInTheDocument();
    expect(within(detail).queryByText("孤立笔记")).not.toBeInTheDocument();
  });

  it("从详情卡点邻接实体可切换选中（图谱内漫游）", async () => {
    stubGraphFetch(FIXTURE);
    const user = userEvent.setup();
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    await user.click(container.querySelector('[data-graph-node="p1"]') as Element);
    const detail = await screen.findByRole("complementary");
    await user.click(within(detail).getByText("力导向布局"));

    // 选中切到 k1：详情卡标题变为 力导向布局，且其唯一邻接是 p1
    const nextDetail = await screen.findByRole("complementary");
    expect(within(nextDetail).getAllByText("力导向布局").length).toBeGreaterThan(0);
    expect(within(nextDetail).getByText("Bandi Agent")).toBeInTheDocument();
  });

  it("空图谱走空态引导文案（不是空白列表）", async () => {
    stubGraphFetch({ entities: [], edges: [] });
    await renderGraphPage();

    expect(await screen.findByText("还没有图谱数据")).toBeInTheDocument();
  });

  it("接口失败走错误态并给出重试入口（不白屏）", async () => {
    stubGraphFetch({ detail: "boom" }, false, 500);
    await renderGraphPage();

    expect(await screen.findByText("图谱加载失败")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("滚轮在画布上触发缩放（走自注册的 non-passive 监听；React 的 onWheel 是 passive，preventDefault 无效）", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    const canvas = container.querySelector('svg[aria-label="知识图谱画布"]')
      ?.parentElement as HTMLElement;
    const graphGroup = container.querySelector('svg[aria-label="知识图谱画布"] g') as SVGGElement;
    const before = graphGroup.getAttribute("transform");

    // 小图 fitToBounds 已顶到 MAX_SCALE，放大方向会被夹取 → 用缩小方向（deltaY>0）验证监听生效
    // 重负载下 act 调度可能慢/监听挂载晚：轮询派发直至视图变化（上限 8s，Day139/Day136 偶发假红）
    await waitFor(
      () => {
        const wheelEvent = new Event("wheel", { bubbles: true, cancelable: true });
        Object.defineProperty(wheelEvent, "deltaY", { value: 120 });
        Object.defineProperty(wheelEvent, "clientX", { value: 100 });
        Object.defineProperty(wheelEvent, "clientY", { value: 100 });
        canvas.dispatchEvent(wheelEvent);
        expect(graphGroup.getAttribute("transform")).not.toBe(before);
      },
      { timeout: 8000, interval: 500 },
    );
  });

  it("实体超过渲染上限时按度数折叠，并以带标签的数字如实提示", async () => {
    const entities = Array.from({ length: 900 }, (_, index) => ({
      id: `e${index}`,
      type: "note",
      name: `实体${index}`,
    }));
    // e0 是 120 度的枢纽，必然落在保留集合内
    const edges = Array.from({ length: 120 }, (_, index) => ({
      source: "e0",
      target: `e${index + 1}`,
      type: "references",
    }));
    stubGraphFetch({ entities, edges });

    const { container } = await renderGraphPage();

    await waitFor(
      () => {
        expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(800);
      },
      { timeout: 20000 },
    );
    expect(screen.getByText("实体过多，已按连接度折叠显示")).toBeInTheDocument();
    expect(screen.getByText("实体 +100 · 关系 +0")).toBeInTheDocument();
  });

  it("纯点击走 pin→unpin 但不触发 settle（点选不全图晃动），click 后仍选中", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    const node = container.querySelector('[data-graph-node="p1"]') as Element;
    firePointer(node, "pointerdown", { clientX: 5, clientY: 5 });
    firePointer(node, "pointerup", { clientX: 5, clientY: 5 });
    fireEvent.click(node);

    const detail = await screen.findByRole("complementary");
    expect(within(detail).getByText("Bandi Agent")).toBeInTheDocument();
  });

  it("拖拽节点（位移超过 CLICK_SLOP）结束时 click 被抑制，不误选中", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    const node = container.querySelector('[data-graph-node="p1"]') as Element;
    firePointer(node, "pointerdown", { clientX: 5, clientY: 5 });
    firePointer(node, "pointermove", { clientX: 60, clientY: 60 });
    firePointer(node, "pointerup", { clientX: 60, clientY: 60 });
    fireEvent.click(node);

    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  });

  it("搜索过滤收窄画布（命中 + 一跳邻域），并如实提示隐藏计数（搜索词由顶栏 store 下发）", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    act(() => useKnowledgeStore.setState({ query: "bandi" }));

    // 命中 p1，邻域带进 t1/k1；n1 无关 → 3 个节点
    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(3);
    });
    expect(screen.getByText(/按名称过滤显示/)).toBeInTheDocument();
    expect(screen.getByText(/−1/)).toBeInTheDocument();
  });

  it("搜索无命中走「没有匹配」态（语义区别于库为空）", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    act(() => useKnowledgeStore.setState({ query: "不存在的词" }));

    expect(await screen.findByText("没有匹配的实体")).toBeInTheDocument();
    expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(0);
  });

  it("清空搜索恢复全图", async () => {
    stubGraphFetch(FIXTURE);
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    act(() => useKnowledgeStore.setState({ query: "bandi" }));
    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(3);
    });

    act(() => useKnowledgeStore.getState().resetQuery());
    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });
  });

  it("实体卡 chip 点击 = 选中 + 画布定位（目标实体居中）", async () => {
    stubGraphFetch(FIXTURE);
    const user = userEvent.setup();
    const { container } = await renderGraphPage();

    await waitFor(() => {
      expect(container.querySelectorAll("[data-graph-node]")).toHaveLength(4);
    });

    await user.click(container.querySelector('[data-graph-node="p1"]') as Element);
    const detail = await screen.findByRole("complementary");
    await user.click(within(detail).getByText("力导向布局"));

    // 选中切到 k1
    expect(await screen.findByRole("complementary")).toBeInTheDocument();

    // 定位：k1 的屏幕坐标应落在画布中心（公式与 GraphCanvas 一致）
    const graphGroup = container.querySelector("svg > g") as SVGGElement;
    const match = /translate\((-?[\d.]+) (-?[\d.]+)\) scale\((-?[\d.]+)\)/.exec(
      graphGroup.getAttribute("transform") ?? "",
    );
    expect(match).not.toBeNull();

    const { LAYOUT_CANVAS, layoutGraph } = await import("@/lib/graph-layout");
    const { normalizeSnapshot } = await import("@/services/graph");
    const layout = layoutGraph(normalizeSnapshot(FIXTURE));
    const point = layout.positions.get("k1");
    expect(point).toBeDefined();

    const [, tx, ty, scale] = match as unknown as string[];
    const viewScale = Number(scale);
    const screenX = point!.x * viewScale + Number(tx);
    const screenY = point!.y * viewScale + Number(ty);
    expect(Math.abs(screenX - LAYOUT_CANVAS.width / 2)).toBeLessThan(1);
    expect(Math.abs(screenY - LAYOUT_CANVAS.height / 2)).toBeLessThan(1);
  });
});

/**
 * 功能：派发带 pointerId 的 pointer 事件。
 * jsdom 26 没有 PointerEvent（fireEvent.pointerDown 降级为普通 Event，`button`/`pointerId`
 * 会被丢弃），而产品代码以 `event.button`/`event.pointerId` 分流 —— 用 MouseEvent +
 * defineProperty 构造出真浏览器等价的形状。注意：jsdom 不做 pointer capture 重定向
 * click 目标语义（capture 破坏 onClick 的规范行为）无法在此覆盖，见交付文档残余。
 */
function firePointer(
  target: Element,
  type: "pointerdown" | "pointermove" | "pointerup",
  coords: { clientX: number; clientY: number },
): void {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, ...coords });
  Object.defineProperty(event, "pointerId", { value: 7 });
  fireEvent(target, event);
}
