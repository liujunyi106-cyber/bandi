// 功能：启动阶段的进度口径——把主进程上报的真实阶段映射成进度条百分比。

/** 功能：壳上报的启动阶段，顺序即真实推进顺序。 */
export type BootStage = "spawn" | "listening" | "probing" | "ready";

/**
 * 功能：每个阶段完成时的百分比。
 *
 * 权重按真实耗时分布：拉起解释器最慢（进程冷启动占大头），端口监听次之，
 * 依赖探针通常在监听后很快返回。数值只决定进度条的视觉节奏，
 * 阶段判定本身全部来自主进程实测信号，不是计时估算。
 */
export const BOOT_STAGE_PROGRESS: Record<BootStage, number> = {
  spawn: 10,
  listening: 40,
  probing: 85,
  ready: 100,
};

/** 功能：阶段顺序表，用于求「当前阶段的下一个里程碑」。 */
const STAGE_ORDER: BootStage[] = ["spawn", "listening", "probing", "ready"];

/**
 * 功能：把「阶段 + 阶段内亚进度」折算成百分比。
 *
 * 亚进度（0~1）只用于同一阶段内平滑推进——没有真实亚进度时传 0 即停在
 * 该阶段起点，绝不越过下一阶段起点。就绪恒为 100%，未知阶段回落最小
 * 可见值（不得假装就绪）。
 */
export function bootProgressOf(stage: BootStage, sub = 0): number {
  const start = BOOT_STAGE_PROGRESS[stage];
  if (start === undefined) return BOOT_STAGE_PROGRESS.spawn;
  if (stage === "ready") return 100;

  const clamped = Math.min(1, Math.max(0, sub));
  const index = STAGE_ORDER.indexOf(stage);
  const next = BOOT_STAGE_PROGRESS[STAGE_ORDER[index + 1]];
  const floor = Math.round(start * (1 - 0) );
  const value = start + (next - start) * clamped * 0.92;
  return Math.max(floor, Math.min(99, Math.round(value)));
}
