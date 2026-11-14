import { Loader2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

const POLL_INTERVAL_MS = 300;
const TIMEOUT_SEC = 60;

/** 功能：读壳在主进程决定的启动阶段（`file:` 降级壳用 ?boot=starting|failed 传入）。 */
function bootPhaseFromLocation(): "starting" | "failed" | null {
  const value = new URLSearchParams(window.location.search).get("boot");
  return value === "starting" || value === "failed" ? value : null;
}

/** 功能：本地内核就绪门——启动期显示进度，避免页面先发请求导致「卡死」观感。 */
export function KernelGate({ children }: { children: ReactNode }) {
  const t = useT();
  const bootPhase = bootPhaseFromLocation();
  const [state, setState] = useState<"checking" | "ready" | "timeout" | "unavailable">(
    bootPhase === "failed" ? "unavailable" : "checking",
  );
  const [elapsed, setElapsed] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // file: 静态降级壳没有同源后端可探——壳已判定 sidecar 起不来时不得假装就绪，
    // 否则用户看到的是完整界面却所有请求都失败。仅显式 boot=starting 视为就绪中。
    // 该判定先于测试短路：降级壳的可用性由壳决定，与运行环境无关。
    if (window.location.protocol === "file:") {
      setState(bootPhase === "starting" ? "checking" : "unavailable");
      return;
    }
    if (import.meta.env.MODE === "test") {
      setState("ready");
      return;
    }
    let cancelled = false;
    const startedAt = Date.now();
    setState("checking");
    setElapsed(0);

    const poll = async () => {
      try {
        const response = await fetch("/health", { cache: "no-store" });
        const contentType = response.headers.get("content-type") ?? "";
        if (response.ok && contentType.includes("application/json")) {
          if (!cancelled) setState("ready");
          return;
        }
      } catch {
        // 内核尚未监听：继续轮询
      }
      if (cancelled) return;
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      setElapsed(seconds);
      if (seconds >= TIMEOUT_SEC) {
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
  }, [attempt]);

  if (state === "ready") return <>{children}</>;

  const failed = state === "timeout" || state === "unavailable";
  const message =
    state === "unavailable"
      ? t("app.kernelUnavailable")
      : state === "timeout"
        ? t("app.kernelTimeout")
        : t("app.kernelStarting");

  return (
    <div className="bg-background text-foreground flex min-h-screen flex-col items-center justify-center gap-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        {state === "checking" ? (
          <Loader2 className="size-4 animate-spin" aria-hidden="true" />
        ) : null}
        <span>{message}</span>
      </div>
      {state === "checking" ? (
        <p className="text-muted-foreground text-xs">
          {t("app.kernelElapsed").replace("{n}", String(elapsed))}
        </p>
      ) : null}
      {failed ? (
        <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>
          {t("app.kernelRetry")}
        </Button>
      ) : null}
    </div>
  );
}
