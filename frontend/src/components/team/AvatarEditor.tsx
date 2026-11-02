import { Check, ImagePlus } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { AvatarShape } from "@/components/team/AvatarShape";
import { buttonVariants } from "@/components/ui/button";
import { useT } from "@/i18n";
import {
  AVATAR_SHAPES,
  AVATAR_TONE_CLASSES,
  AVATAR_TONE_COLORS,
  type AvatarShapeId,
} from "@/lib/employee-avatar";
import { cn } from "@/lib/utils";
import { companyApi } from "@/services/company";
import { describeError } from "@/services/team";

export interface AvatarEditorValue {
  tone: number;
  shape: AvatarShapeId;
  avatarUrl: string;
}

const AVATAR_MIME_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
const AVATAR_EXTENSIONS = new Set(["png", "jpg", "jpeg", "webp"]);

function isAvatarFile(file: File): boolean {
  if (AVATAR_MIME_TYPES.has(file.type)) return true;
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return AVATAR_EXTENSIONS.has(extension);
}

interface AvatarEditorProps extends AvatarEditorValue {
  name: string;
  onChange: (value: AvatarEditorValue) => void;
}

/** 功能：员工头像编辑器（颜色 / 形状 / 自定义上传）。 */
export function AvatarEditor({ name, tone, shape, avatarUrl, onChange }: AvatarEditorProps) {
  const t = useT();
  const [uploading, setUploading] = useState(false);
  const toneClass = AVATAR_TONE_CLASSES[tone % AVATAR_TONE_CLASSES.length];
  const toneColor = AVATAR_TONE_COLORS[tone % AVATAR_TONE_COLORS.length];
  const generatedShape = !avatarUrl && shape !== "classic";

  const selectTone = (nextTone: number) => {
    onChange({ tone: nextTone, shape, avatarUrl: "" });
  };

  const selectShape = (nextShape: AvatarShapeId) => {
    onChange({ tone, shape: nextShape, avatarUrl: "" });
  };

  const uploadAvatar = async (file: File) => {
    if (!isAvatarFile(file)) {
      toast.error(t("team.add.avatarInvalid"));
      return;
    }
    setUploading(true);
    try {
      const result = await companyApi.upload(file);
      onChange({ tone, shape, avatarUrl: companyApi.uploadUrl(result.upload.upload_id) });
      toast.info(t("team.add.avatarPending"));
    } catch (error) {
      toast.error(describeError(error, t("team.add.avatarFailed")));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="flex items-stretch gap-4" data-testid="avatar-editor">
      <div
        className={cn(
          "grid w-20 shrink-0 place-items-center text-2xl font-semibold",
          avatarUrl
            ? "bg-primary/5 overflow-hidden rounded-2xl"
            : generatedShape
              ? ""
              : cn("overflow-hidden rounded-2xl", toneClass),
        )}
        style={generatedShape ? { color: toneColor } : undefined}
      >
        {avatarUrl ? (
          <img src={avatarUrl} alt="" className="size-full object-cover" />
        ) : generatedShape ? (
          <AvatarShape shape={shape} className="size-full" />
        ) : (
          name.trim().slice(0, 1) || "?"
        )}
      </div>

      <div className="flex min-w-0 flex-1 flex-col justify-between gap-4">
        <div
          className="flex flex-wrap items-center gap-1.5"
          role="group"
          aria-label={t("team.add.avatarTone")}
        >
          {AVATAR_TONE_CLASSES.map((className, index) => {
            const selected = !avatarUrl && tone === index;
            return (
              <button
                key={className}
                type="button"
                aria-label={`${t("team.add.avatarTone")} ${index + 1}`}
                aria-pressed={selected}
                data-testid={`avatar-tone-${index}`}
                onClick={() => selectTone(index)}
                className={cn(
                  "grid size-7 place-items-center rounded-full transition-shadow",
                  className,
                  selected && "ring-2 ring-foreground ring-inset",
                )}
              >
                {selected ? (
                  <Check
                    className={cn(
                      "size-3",
                      className.includes("text-yellow-950") ? "text-yellow-950" : "text-white",
                    )}
                    strokeWidth={3}
                  />
                ) : null}
              </button>
            );
          })}
        </div>

        <div className="flex items-end gap-2">
          <div
            className="flex min-w-0 flex-1 flex-wrap items-center gap-1"
            role="group"
            aria-label={t("team.add.avatarStyle")}
          >
            <button
              type="button"
              aria-label={t("team.add.shapeClassic")}
              aria-pressed={!avatarUrl && shape === "classic"}
              data-testid="avatar-shape-classic"
              onClick={() => selectShape("classic")}
              className={cn(
                "grid size-9 place-items-center rounded-lg border text-[11px] font-semibold",
                !avatarUrl && shape === "classic"
                  ? "border-foreground bg-chat-rail text-chat-ink"
                  : "border-chat-hairline text-chat-muted hover:bg-chat-rail/60",
              )}
            >
              {name.trim().slice(0, 1) || "?"}
            </button>
            {AVATAR_SHAPES.map((item) => {
              const selected = !avatarUrl && shape === item.id;
              return (
                <button
                  key={item.id}
                  type="button"
                  aria-label={t(item.labelKey)}
                  aria-pressed={selected}
                  data-testid={`avatar-shape-${item.id}`}
                  onClick={() => selectShape(item.id)}
                  className={cn(
                    "grid size-9 place-items-center rounded-lg border transition-colors",
                    selected
                      ? "border-foreground bg-chat-rail"
                      : "border-chat-hairline hover:bg-chat-rail/60",
                  )}
                >
                  <AvatarShape shape={item.id} className="size-6" style={{ color: toneColor }} />
                </button>
              );
            })}
          </div>

          <label
            className={cn(
              buttonVariants({ variant: "outline", size: "sm" }),
              "shrink-0 cursor-pointer",
              uploading && "pointer-events-none opacity-50",
            )}
            role="button"
            tabIndex={uploading ? -1 : 0}
            aria-disabled={uploading}
          >
            <ImagePlus />
            {uploading ? t("team.add.uploading") : t("team.add.uploadAvatar")}
            <input
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="sr-only"
              disabled={uploading}
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void uploadAvatar(file);
                event.target.value = "";
              }}
            />
          </label>
        </div>
      </div>
    </div>
  );
}
