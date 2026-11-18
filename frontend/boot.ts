/**
 * 功能：启动页脚本——承载体是独立文档 boot.html，不加载 React/桌面端样式。
 *
 * 三个动画状态按真实链路推进，共用同一条进度条：
 *   ① 等待　三黑点动画（boot-pending.webp，还没收到内核阶段信号）
 *   ② 进度　真实进度（spawn → listening → probing）
 *   ③ 完成　预示动画（进度满格 + 切 boot-done.webp，停留后进桌面端）
 *
 * 进度来源是主进程实测阶段，不是计时估算；阶段只增不减。
 */

import bootDoneUrl from "@/assets/boot/boot-done.webp";
import bootPendingUrl from "@/assets/boot/boot-pending.webp";

/** 功能：阶段到进度百分比的映射，覆盖整条启动链路（内核 0→100）。 */
const STAGE_PERCENT: Record<string, number> = {
  spawn: 8,
  listening: 32,
  probing: 70,
  ready: 100,
};

/** 功能：完成预示动画的停留时长——满格后让用户看到「好了，正在进入」。 */
const DONE_HOLD_MS = 900;

/**
 * 功能：阶段内亚进度推进——真实阶段信号之间常有几秒空档（起解释器最慢），
 * 不补会让进度条长时间停在同一格，看起来像卡死。亚进度只在本阶段区间内滑动，
 * 永远不越过下一阶段起点——阶段判定仍以主进程实测信号为准。
 */
const STAGE_ORDER = ["spawn", "listening", "probing", "ready"];

/** 功能：亚进度刷新间隔——每次只走一小步，接近区间上限时收敛。 */
const TICK_MS = 420;

/** 功能：本阶段的亚进度上限（占区间的比例），余量留给真实信号。 */
const SUB_MAX = 0.9;

const fill = document.getElementById("fill");
const caption = document.getElementById("caption");
const anim = document.getElementById("anim") as HTMLImageElement | null;

let current = 0;
let finished = false;
let subTimer: number | null = null;
let subProgress = 0;

/** 功能：取某阶段的进度起点。 */
function stageFloor(stage: string): number {
  return STAGE_PERCENT[stage] ?? 0;
}

/** 功能：取某阶段的进度上限（下一阶段起点）。 */
function stageCeil(stage: string): number {
  const i = STAGE_ORDER.indexOf(stage);
  if (i < 0 || i >= STAGE_ORDER.length - 1) return 100;
  return stageFloor(STAGE_ORDER[i + 1]);
}

/** 功能：停掉亚进度计时器（阶段推进或完成时调用）。 */
function stopSubProgress(): void {
  if (subTimer !== null) {
    window.clearInterval(subTimer);
    subTimer = null;
  }
}

/**
 * 功能：在当前阶段区间内平滑推进进度。
 *
 * 只在「还没收到下一阶段信号」时走；一旦下一阶段到达就停下并让位给真实进度。
 */
function startSubProgress(stage: string): void {
  stopSubProgress();
  subProgress = 0;
  const floor = stageFloor(stage);
  const ceil = stageCeil(stage);
  if (ceil <= floor) return;
  subTimer = window.setInterval(() => {
    if (finished) return;
    subProgress = Math.min(SUB_MAX, subProgress + 0.06);
    const target = floor + (ceil - floor) * subProgress;
    setPercent(Math.min(target, ceil - 1));
  }, TICK_MS);
}

/** 功能：把进度推到目标值（只增不减）。 */
function setPercent(value: number): void {
  const next = Math.max(current, Math.min(100, Math.round(value)));
  if (next === current && current !== 0) return;
  current = next;
  if (fill) fill.style.width = `${next}%`;
}

/** 功能：按阶段名推进进度；未知阶段忽略。 */
function applyStage(stage: string): void {
  const percent = STAGE_PERCENT[stage];
  if (typeof percent !== "number" || finished) return;
  stopSubProgress();
  setPercent(percent);
  if (percent >= 100) {
    finish();
    return;
  }
  startSubProgress(stage);
}

/**
 * 功能：完成预示动画——进度满格、动画切完成图，停一小段再通知主进程导航。
 *
 * 主进程收到后才 loadURL 到 SPA；SPA 带?boot=ready 直接出内容。
 */
function finish(): void {
  if (finished) return;
  finished = true;
  stopSubProgress();
  setPercent(100);
  if (anim) anim.src = bootDoneUrl;
  if (caption) caption.textContent = "即将进入...";
  const bridge = (window as unknown as { bandi?: { notifyBootDone?: () => void } }).bandi;
  window.setTimeout(() => {
    if (bridge && typeof bridge.notifyBootDone === "function") {
      bridge.notifyBootDone();
    } else if (caption) {
      caption.textContent = "内核未就绪，请重启应用";
    }
  }, DONE_HOLD_MS);
}

if (anim) anim.src = bootPendingUrl;

const bridge = (window as unknown as {
  bandi?: { onBootStage?: (cb: (stage: string) => void) => () => void };
}).bandi;
const subscribed = Boolean(bridge && typeof bridge.onBootStage === "function");
bridge?.onBootStage?.((stage) => applyStage(stage));

// 收不到阶段时给一个可见的最小基线，避免一直停在 0% 让人以为卡死。
setPercent(STAGE_PERCENT.spawn);
// 初始阶段也要起亚进度：否则拉起解释器的那几秒里进度条完全不动，看起来像卡死。
startSubProgress("spawn");
if (!subscribed && caption) caption.textContent = "等待内核...";

(window as unknown as { __bandiBoot?: unknown }).__bandiBoot = {
  applyStage,
  finish,
  getPercent: () => current,
};
