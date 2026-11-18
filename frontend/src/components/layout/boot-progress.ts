// 功能：启动阶段的进度口径——把整条启动链路映射成一条进度条的百分比。

/** 功能：壳上报的启动阶段，顺序即真实推进顺序。 */
export type BootStage = "spawn" | "listening" | "probing" | "ready";

/**
 * 功能：每个阶段完成时的百分比——覆盖**整条启动链路**，不只是内核。
 *
 * 链路真实构成（按耗时占比分配）：
 *   ① spawn      拉起 Python 解释器 + 导入依赖   → 权重最大
 *   ② listening  监听端口、Uvicorn 就绪
 *   ③ probing    依赖探针（/ready）
 *   ④ warming    首屏数据预热（会话/员工/项目）  ← 也是加载的一部分
 *   ⑤ ready      交出桌面端
 *
 * 关键约束：内核 ready **不等于**界面可用。若把内核 ready 画成 100%，用户会在
 * 进度条满格后继续等数据加载；因此内核只占 85%，余下 15% 留给首屏预热。
 */
export const BOOT_STAGE_PROGRESS: Record<BootStage, number> = {
  spawn: 8,
  listening: 32,
  probing: 70,
  ready: 85,
};

/** 功能：首屏数据预热阶段占用的进度区间（内核就绪 → 界面可用）。 */
export const WARMUP_PROGRESS: { start: number; end: number } = { start: 85, end: 100 };

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
  // ready 是「内核就绪」不是「界面可用」：停在 85%，余下由首屏预热推到 100。
  if (stage === "ready") return BOOT_STAGE_PROGRESS.ready;

  const clamped = Math.min(1, Math.max(0, sub));
  const index = STAGE_ORDER.indexOf(stage);
  const next = BOOT_STAGE_PROGRESS[STAGE_ORDER[index + 1]];
  const floor = Math.round(start * (1 - 0) );
  const value = start + (next - start) * clamped * 0.92;
  return Math.max(floor, Math.min(99, Math.round(value)));
}
