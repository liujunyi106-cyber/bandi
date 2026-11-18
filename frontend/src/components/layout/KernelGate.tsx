/**
 * 功能：内核就绪门——SPA 只在内核可用且首屏数据备好之后才渲染业务界面。
 *
 * 启动画面本身是独立文档 `boot.html`（三黑点等待 → 真实进度 → 完成预示动画），
 * 播完由主进程导航到 `?boot=ready`。本组件是那次导航的落地页，因此**不再演启动
 * 动画**：它只做两件事——确认内核活着、把首屏数据拉起来，然后交出内容。
 *
 * 之所以拆开：启动动画和桌面端在同一个页面里会让「播完动画 → 导航 → 新页面
 * 重新挂载」变成两遍过场。独立文档之后，启动阶段不加载 React，桌面端也不重播动画。
 */

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

import { warmupFirstScreen } from "@/components/layout/boot-warmup";

/** 功能：轮询间隔——内核通常秒级起来，300ms 足够灵敏又不吵。 */
const POLL_INTERVAL_MS = 300;

/** 功能：等待上限——超过即判定内核没起来，显示重试而不是无限转圈。 */
const TIMEOUT_SEC = 90;

/** 功能：解析 URL 上的 boot 参数（壳注入的启动上下文）。 */
function bootPhaseFromLocation(): "ready" | "failed" | "other" {
  const value = new URLSearchParams(window.location.search).get("boot");
  if (value === "failed") return "failed";
  if (value === "ready") return "ready";
  return "other";
}

type GateState = "checking" | "ready" | "timeout" | "unavailable";

/** 功能：本地内核就绪门——连上并备好首屏数据后渲染业务界面。 */
export function KernelGate({ children }: { children: ReactNode }) {
  const t = useT();
  const bootPhase = bootPhaseFromLocation();
  const [state, setState] = useState<GateState>(
    bootPhase === "failed" ? "unavailable" : "checking",
  );
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // 壳已判定 sidecar 起不来：不得假装就绪，否则用户看到完整界面却所有请求都失败。
    if (bootPhase === "failed") {
      setState("unavailable");
      return;
    }
    // 测试环境不探活（无真实后端），直接放行由用例驱动。
    if (import.meta.env.MODE === "test") {
      setState("ready");
      return;
    }

    let cancelled = false;
    const startedAt = Date.now();
    setState("checking");

    const poll = async () => {
      try {
        const response = await fetch("/health", { cache: "no-store" });
        const contentType = response.headers.get("content-type") ?? "";
        if (response.ok && contentType.includes("application/json")) {
          if (cancelled) return;
          // 内核活着只代表接口可用：等首屏数据落位再交出内容，避免进桌面端先白屏。
          await warmupFirstScreen().catch(() => undefined);
          if (!cancelled) setState("ready");
          return;
        }
      } catch {
        // 内核尚未监听：继续轮询
      }
      if (cancelled) return;
      if (Math.floor((Date.now() - startedAt) / 1000) >= TIMEOUT_SEC) {
        setState("timeout");
        return;
      }
      timerRef.current = setTimeout(poll, POLL_INTERVAL_MS);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [bootPhase]);

  const failed = state === "timeout" || state === "unavailable";

  // 就绪与否都先渲染界面：此时已经在内核上跑，空白屏幕比少几个数据更糟。
  // 就绪前用一层透明遮罩终止交互，避免用户在数据未到时点出错误操作。
  if (!failed) {
    return (
      <>
        {children}
        {state !== "ready" ? (
          <div
            className="fixed inset-0 cursor-wait"
            data-testid="kernel-gate"
            aria-busy="true"
          />
        ) : null}
      </>
    );
  }

  return (
    <div
      className="bg-background text-foreground fixed inset-0 grid place-items-center overflow-hidden"
      data-testid="kernel-gate"
    >
      <div className="flex w-[min(46vw,13rem)] flex-col items-center gap-3">
        <p className="text-muted-foreground text-center text-[11px]">
          {state === "unavailable" ? t("app.kernelUnavailable") : t("app.kernelTimeout")}
        </p>
        <Button size="sm" variant="outline" onClick={() => setState("checking")}>
          {t("app.kernelRetry")}
        </Button>
      </div>
    </div>
  );
}
