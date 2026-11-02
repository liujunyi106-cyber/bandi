/** 功能：员工头像预设与元数据解析（与创建向导、卡片共用）。 */

export const AVATAR_TONE_CLASSES = [
  "bg-primary text-primary-foreground",
  "bg-blue-600 text-white",
  "bg-teal-600 text-white",
  "bg-amber-500 text-white",
  "bg-violet-600 text-white",
  "bg-rose-600 text-white",
  "bg-[#8b5e3c] text-white",
  "bg-yellow-400 text-yellow-950",
  "bg-stone-500 text-white",
  "bg-cyan-500 text-white",
] as const;

export const AVATAR_TONE_COLORS = [
  "var(--primary)",
  "#2563eb",
  "#0d9488",
  "#f59e0b",
  "#7c3aed",
  "#e11d48",
  "#8b5e3c",
  "#facc15",
  "#78716c",
  "#06b6d4",
] as const;

export const AVATAR_SHAPES = [
  { id: "capsule", labelKey: "team.add.shapeCapsule" },
  { id: "cloud", labelKey: "team.add.shapeCloud" },
  { id: "egg", labelKey: "team.add.shapeEgg" },
  { id: "bean", labelKey: "team.add.shapeBean" },
  { id: "waterdrop", labelKey: "team.add.shapeWaterdrop" },
  { id: "leaf", labelKey: "team.add.shapeLeaf" },
  { id: "dango", labelKey: "team.add.shapeDango" },
] as const;

export type AvatarShapeId = "classic" | (typeof AVATAR_SHAPES)[number]["id"];

export interface EmployeeAvatarMeta {
  kind: "tone" | "upload";
  tone: number;
  shape: AvatarShapeId;
  url: string;
}

const SHAPE_IDS = new Set<string>(AVATAR_SHAPES.map((item) => item.id));

/** 功能：按姓名/标识稳定取一组预设配色（同一员工刷新、换机都不变）。 */
export function hashTone(seed: string): number {
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) >>> 0;
  }
  return hash % AVATAR_TONE_CLASSES.length;
}

/** 功能：从员工 metadata 读取头像配置（缺省 classic = 首字 + 默认底）。 */
export function avatarMeta(metadata?: Record<string, unknown> | null): EmployeeAvatarMeta | null {
  const raw = metadata?.avatar;
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const kind = record.kind === "upload" ? "upload" : "tone";
  const tone = Number.isFinite(Number(record.tone)) ? Number(record.tone) : 0;
  const shape =
    typeof record.shape === "string" && SHAPE_IDS.has(record.shape)
      ? (record.shape as AvatarShapeId)
      : "classic";
  return { kind, tone, shape, url: typeof record.url === "string" ? record.url : "" };
}
