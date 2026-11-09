import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

/**
 * 功能：补 jsdom 缺失的 `window.matchMedia`。
 * jsdom 未实现该 API（`window.matchMedia is not a function`），而产品代码用它做
 * 断点折叠与 reduced-motion 终态判定 —— 不补桩会让任何渲染到侧栏/画布的组件测试直接崩。
 * 默认 `matches: false`（等同桌面宽屏 + 无 reduced-motion），个别用例可覆写。
 */
// 用 globalThis 而非 window：本文件会被 tsconfig.node（无 DOM lib）的类型检查覆盖
const globalWindow = globalThis as typeof globalThis & {
  window?: typeof globalThis & { matchMedia?: (query: string) => unknown };
};

if (globalWindow.window && typeof globalWindow.window.matchMedia !== "function") {
  Object.defineProperty(globalWindow.window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

/**
 * 功能：用 16ms 定时器模拟 `requestAnimationFrame`（测试侧等价帧语义）。
 * jsdom 的 rAF 不保证推进（vitest jsdom 环境下回调可能永不执行），而图谱拖拽的
 * 实时重排由 rAF 帧循环驱动——不补桩会让「拖拽帧推进」类行为无法断言（137b-3）。
 */
const rafGlobal = globalThis as typeof globalThis & {
  requestAnimationFrame?: (callback: (time: number) => void) => number;
  cancelAnimationFrame?: (handle: number) => void;
};

rafGlobal.requestAnimationFrame = (callback) =>
  setTimeout(() => callback(performance.now()), 16) as unknown as number;
rafGlobal.cancelAnimationFrame = (handle) => clearTimeout(handle as unknown as number);

/**
 * 功能：补 jsdom 缺失的 ResizeObserver（Radix ScrollArea 依赖）。
 * 不补桩时审批面板一打开就抛 `ResizeObserver is not defined`，整棵组件树卸载。
 */
const resizeGlobal = globalThis as typeof globalThis & {
  ResizeObserver?: new () => { observe: () => void; unobserve: () => void; disconnect: () => void };
};
if (!resizeGlobal.ResizeObserver) {
  resizeGlobal.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
