/**
 * 功能：MessageList 滚动锚定行为测试（-3 ：
 * 切换会话定位最新 / 回到底部按钮显示逻辑 / 贴底判定）。
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MessageList } from "@/components/chat/MessageList";
import type { ChatMessage } from "@/stores/chat-store";

function message(id: string): ChatMessage {
  return { id, sessionId: "s1", role: "user", content: `消息 ${id}`, done: true };
}

/** jsdom 无布局——注入 scrollHeight/clientHeight 指标（scrollTop 可写）。 */
function fakeMetrics(el: HTMLElement, scrollHeight = 1000, clientHeight = 300) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, value: scrollHeight });
  Object.defineProperty(el, "clientHeight", { configurable: true, value: clientHeight });
}

describe("MessageList 滚动锚定（137b-3）", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("破坏者：用户上滚（距底 >80px）显示「回到底部」，点击后滚底并隐藏", () => {
    render(<MessageList messages={[message("m1"), message("m2")]} />);
    const list = screen.getByTestId("message-list");
    fakeMetrics(list);
    list.scrollTop = 100; // distance = 1000 - 100 - 300 = 600 > 80
    fireEvent.scroll(list);

    const button = screen.getByTestId("scroll-to-bottom");
    expect(button).toBeInTheDocument();

    fireEvent.click(button);
    // 到底部语义（scrollTop 被钳到 scrollHeight-clientHeight；jsdom 不钳）
    expect(list.scrollTop).toBeGreaterThanOrEqual(1000 - 300);
    expect(screen.queryByTestId("scroll-to-bottom")).not.toBeInTheDocument();
  });

  it("用户贴底（distance ≤ 80px）不显示按钮", () => {
    render(<MessageList messages={[message("m1")]} />);
    const list = screen.getByTestId("message-list");
    fakeMetrics(list);
    list.scrollTop = 750; // distance = -50 ≤ 80
    fireEvent.scroll(list);
    expect(screen.queryByTestId("scroll-to-bottom")).not.toBeInTheDocument();
  });

  it("切换会话（sessionKey 变化）→ 直接定位最新（底部）并隐藏按钮", () => {
    const { rerender } = render(<MessageList messages={[message("m1")]} sessionKey="s1" />);
    const list = screen.getByTestId("message-list");
    fakeMetrics(list);
    list.scrollTop = 100;
    fireEvent.scroll(list);
    expect(screen.getByTestId("scroll-to-bottom")).toBeInTheDocument();

    rerender(<MessageList messages={[message("m1")]} sessionKey="s2" />);
    expect(list.scrollTop).toBeGreaterThanOrEqual(1000 - 300);
    expect(screen.queryByTestId("scroll-to-bottom")).not.toBeInTheDocument();
  });

  it("内容尺寸变化（决策卡出现/消失）→ 贴底保持（ResizeObserver 不跳动）", () => {
    const observers: ResizeObserverCallback[] = [];
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        observers.push(callback);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);

    render(<MessageList messages={[message("m1")]} />);
    const list = screen.getByTestId("message-list");
    fakeMetrics(list);
    list.scrollTop = 700; // 贴底（distance = 0）
    fireEvent.scroll(list);

    // 决策卡插入 → 内容变高；贴底时应自动跟随（原实现停在原地 → 面板跳动）
    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 900 });
    act(() => {
      observers.forEach((callback) => callback([], {} as ResizeObserver));
    });
    expect(list.scrollTop).toBeGreaterThanOrEqual(900 - 300);
  });

  it("破坏者：用户上滚时尺寸变化不抢滚动位置（不打扰阅读）", () => {
    const observers: ResizeObserverCallback[] = [];
    class FakeResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        observers.push(callback);
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", FakeResizeObserver);

    render(<MessageList messages={[message("m1")]} />);
    const list = screen.getByTestId("message-list");
    fakeMetrics(list);
    list.scrollTop = 120; // 上滚中（stick=false）
    fireEvent.scroll(list);

    Object.defineProperty(list, "scrollHeight", { configurable: true, value: 1400 });
    act(() => {
      observers.forEach((callback) => callback([], {} as ResizeObserver));
    });
    expect(list.scrollTop).toBe(120);
  });
});
