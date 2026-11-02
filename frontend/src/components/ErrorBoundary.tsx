/**
  * 功能：全局错误兜底（@base docs/前端设计规划.md 「React ErrorBoundary 全局错误兜底」）。
 * 面板级崩溃不拖垮整壳——fallback 可用 render prop 注入（业务侧用 useT 做多语言文案）。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

import { Button } from "@/components/ui/button";

interface ErrorBoundaryProps {
  children: ReactNode;
  /** 自定义兜底 UI；不传则用内置中文兜底。 */
  fallback?: (error: Error, reset: () => void) => ReactNode;
  /** 上报钩子（接可观测 / toast）。 */
  onError?: (error: Error, info: ErrorInfo) => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[ErrorBoundary]", error, info.componentStack);
    this.props.onError?.(error, info);
  }

  private reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback(error, this.reset);

    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <h2 className="text-foreground text-lg font-semibold">页面出错了</h2>
        <p className="text-muted-foreground max-w-md text-sm">
          刚才这个面板崩了，其它功能不受影响。
        </p>
        <pre className="text-muted-foreground max-w-lg overflow-auto rounded-md bg-muted px-3 py-2 text-left text-xs">
          {error.message}
        </pre>
        <Button variant="outline" size="sm" onClick={this.reset}>
          重新加载
        </Button>
      </div>
    );
  }
}
