import { Loader2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

const POLL_INTERVAL_MS = 300;
const TIMEOUT_SEC = 60;

/** 功能：本地内核就绪门——启动期显示进度，避免页面先发请求导致「卡死」观感。 */
export function KernelGate({ children }: { children: ReactNode }) {
  const t = useT();
  const [state, setState] = useState<"checking" | "ready" | "timeout">("checking");
  const [elapsed, setElapsed] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (import.meta.env.MODE === "test" || window.location.protocol === "file:") {
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

  return (
    <div className="bg-background text-foreground flex min-h-screen flex-col items-center justify-center gap-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        {state === "checking" ? (
          <Loader2 className="size-4 animate-spin" aria-hidden="true" />
        ) : null}
        <span>{state === "timeout" ? t("app.kernelTimeout") : t("app.kernelStarting")}</span>
      </div>
      {state === "checking" ? (
        <p className="text-muted-foreground text-xs">
          {t("app.kernelElapsed").replace("{n}", String(elapsed))}
        </p>
      ) : (
        <Button size="sm" variant="outline" onClick={() => setAttempt((value) => value + 1)}>
          {t("app.kernelRetry")}
        </Button>
      )}
    </div>
  );
}
