import { AvatarShape } from "@/components/team/AvatarShape";
import {
  AVATAR_TONE_CLASSES,
  AVATAR_TONE_COLORS,
  avatarMeta,
  hashTone,
} from "@/lib/employee-avatar";
import { cn } from "@/lib/utils";

/** 功能：员工头像统一渲染（上传图 / 默认首字 / 可选形状）。 */
export function EmployeeAvatar({
  name,
  metadata,
  className,
  testId,
}: {
  name: string;
  metadata?: Record<string, unknown> | null;
  className?: string;
  testId?: string;
}) {
  const avatar = avatarMeta(metadata);
  // 未配置头像的员工按姓名稳定取色，避免所有人同一个底色
  const tone = avatar?.tone ?? hashTone(name);
  const uploadedUrl = avatar?.kind === "upload" ? avatar.url : "";
  const shape = avatar?.shape ?? "classic";
  const shaped = !uploadedUrl && shape !== "classic";

  return (
    <div
      data-testid={testId}
      className={cn(
        "grid shrink-0 place-items-center overflow-hidden rounded-2xl text-base font-semibold",
        uploadedUrl
          ? "bg-primary/5"
          : shaped
            ? "rounded-none overflow-visible"
            : AVATAR_TONE_CLASSES[tone % AVATAR_TONE_CLASSES.length],
        className,
      )}
      style={shaped ? { color: AVATAR_TONE_COLORS[tone % AVATAR_TONE_COLORS.length] } : undefined}
    >
      {uploadedUrl ? (
        <img src={uploadedUrl} alt="" className="size-full object-cover" />
      ) : shaped ? (
        <AvatarShape shape={shape} className="size-full" />
      ) : (
        name.slice(0, 1)
      )}
    </div>
  );
}
