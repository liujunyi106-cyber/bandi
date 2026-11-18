import { useEffect, useRef, useState, type ReactNode } from "react";

import bootDone from "@/assets/boot/boot-done.webp";
import bootPending from "@/assets/boot/boot-pending.webp";
import { BOOT_STAGE_PROGRESS, bootProgressOf, type BootStage } from "@/components/layout/boot-progress";
import { warmupFirstScreen } from "@/components/layout/boot-warmup";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

const POLL_INTERVAL_MS = 300;
const TIMEOUT_SEC = 60;
const DONE_ANIMATION_MS = 2200;

/** 功能：读壳在主进程决定的启动阶段。
 *
 *  `starting` = 占位页，负责把「三黑点等待 → 进度条 → 完成动画」一次演完；
 *  `ready` = sidecar 已就绪后加载的 SPA，启动画面已经在占位页播过，直接进内容；
 *  `failed` = 壳判定 sidecar 起不来，显示重试页而不是假装可用。
 */
function bootPhaseFromLocation(): "starting" | "ready" | "failed" | null {
  const value = new URLSearchParams(window.location.search).get("boot");
  if (value === "failed") return "failed";
  if (value === "ready") return "ready";
  // starting/waiting 都表示「壳正在拉起 sidecar」，本页负责把这段加载完整演完
  if (value === "starting" || value === "waiting") return "starting";
  return null;
}

/** 功能：订阅主进程上报的真实启动阶段；非桌面壳（浏览器调试）时返回 null。 */
function subscribeBootStage(listener: (stage: BootStage) => void): (() => void) | null {
  const api = (window as unknown as { bandi?: { onBootStage?: unknown } }).bandi;
  const subscribe = api?.onBootStage;
  if (typeof subscribe !== "function") return null;
  return (subscribe as (cb: (stage: string) => void) => () => void)((stage) => {
    if (stage in BOOT_STAGE_PROGRESS) listener(stage as BootStage);
  });
}

/** 功能：本地内核就绪门——启动期显示动画与真实进度，连上并备好首屏数据后进桌面端。 */
export function KernelGate({ children }: { children: ReactNode }) {
  const t = useT();
  const bootPhase = bootPhaseFromLocation();
  const [state, setState] = useState<"checking" | "ready" | "timeout" | "unavailable">(
    bootPhase === "failed" ? "unavailable" : "checking",
  );
  const [stage, setStage] = useState<BootStage>("spawn");
  // 首屏数据是否已备好——后端就绪后还要等这一项，避免「进桌面端先白屏」。
  const [warmed, setWarmed] = useState(bootPhase !== "ready");
  const [done, setDone] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // 启动画面已经在占位页完整播过（三黑点等待 → 进度 → 完成动画），
    // 这里是它加载出来的落地页：不再探活、不再演第二遍，备好数据就交出去。
    if (bootPhase === "ready") {
      setState("ready");
      let cancelled = false;
      void warmupFirstScreen().finally(() => {
        if (!cancelled) setWarmed(true);
      });
      return () => {
        cancelled = true;
      };
    }
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

    const poll = async () => {
      try {
        const response = await fetch("/health", { cache: "no-store" });
        const contentType = response.headers.get("content-type") ?? "";
        if (response.ok && contentType.includes("application/json")) {
          if (!cancelled) {
            setStage("ready");
            setState("ready");
          }
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

  // 真实阶段订阅：优先于轮询驱动进度（阶段来自主进程实测信号）
  useEffect(() => {
    if (import.meta.env.MODE === "test") return;
    const unsubscribe = subscribeBootStage((next) => {
      setStage(next);
      if (next === "ready") setState("ready");
    });
    return () => unsubscribe?.();
  }, []);

  // 就绪后先播完成动画，播完再通知壳「启动画面演完了」——壳随后导航进 SPA，
  // 这一段就是整个启动过程的收尾（三黑点等待 → 进度 → 完成动画 → 桌面端）。
  // 必须等到首屏数据备好（warmed）才开始计时：动画盖住的是真实加载时间，
  // 而不是动画放完再让用户干看一个白屏。
  useEffect(() => {
    if (state !== "ready" || !warmed) return;
    const timer = setTimeout(() => {
      setDone(true);
      const api = (window as unknown as { bandi?: { notifyBootDone?: () => void } }).bandi;
      api?.notifyBootDone?.();
    }, DONE_ANIMATION_MS);
    return () => clearTimeout(timer);
  }, [state, warmed]);

  if (state === "ready" && done) return <>{children}</>;

  const failed = state === "timeout" || state === "unavailable";
  const percent = failed ? BOOT_STAGE_PROGRESS.spawn : bootProgressOf(stage);

  if (failed) {
    return (
      <div className="bg-background text-foreground fixed inset-0 grid place-items-center overflow-hidden">
        <div className="flex w-[min(46vw,13rem)] flex-col items-center gap-3">
          <img src={bootPending} alt="" aria-hidden="true" className="w-full opacity-40 grayscale" />
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

  return (
    <div
      className="bg-background text-foreground fixed inset-0 grid place-items-center overflow-hidden"
      data-testid="kernel-gate"
    >
      {/* 动画、进度条、文案视为一个整体：整列在视口居中，列内三段共用同一宽度基准 */}
      <div className="flex w-[min(46vw,13rem)] flex-col items-center gap-3">
        <img
          src={state === "ready" ? bootDone : bootPending}
          alt=""
          aria-hidden="true"
          className="w-full select-none"
          data-testid="boot-animation"
        />
        <div className="flex w-full flex-col gap-1.5">
          <div
            className="border-primary/25 h-2 w-full overflow-hidden rounded-full border"
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={t("app.bootProgressLabel")}
            data-testid="boot-progress"
          >
            <div
              className="bg-primary h-full rounded-full transition-[width] duration-500 ease-out"
              style={{ width: `${percent}%` }}
            />
          </div>
          <p className="text-muted-foreground text-center text-[11px]" data-testid="boot-caption">
            {t("app.bootLoading")}
          </p>
        </div>
      </div>
    </div>
  );
}
