/**
 * 功能：ErrorBoundary 行为测试。
 * 覆盖：正常渲染 / 抛错兜底 + onError 上报 / reset 恢复。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "@/components/ErrorBoundary";

function Boom({ crash }: { crash: boolean }) {
  if (crash) throw new Error("面板崩溃测试");
  return <div>正常内容</div>;
}

describe("ErrorBoundary", () => {
  it("正常时渲染 children（不触发兜底）", () => {
    render(
      <ErrorBoundary>
        <Boom crash={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("子组件抛错时渲染内置兜底并上报 onError", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const onError = vi.fn();
    render(
      <ErrorBoundary onError={onError}>
        <Boom crash />
      </ErrorBoundary>,
    );
    expect(screen.getByText("页面出错了")).toBeInTheDocument();
    expect(screen.getByText("面板崩溃测试")).toBeInTheDocument();
    expect(onError).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  it("fallback 注入 + reset 后恢复 children（外层数据修复场景）", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    function Wrapper() {
      const [fixed, setFixed] = useState(false);
      return (
        <ErrorBoundary
          fallback={(_error, reset) => (
            <button
              onClick={() => {
                setFixed(true);
                reset();
              }}
            >
              修复并重试
            </button>
          )}
        >
          {fixed ? <div>已恢复</div> : <Boom crash />}
        </ErrorBoundary>
      );
    }
    render(<Wrapper />);
    fireEvent.click(screen.getByRole("button", { name: "修复并重试" }));
    expect(screen.getByText("已恢复")).toBeInTheDocument();
    spy.mockRestore();
  });
});
