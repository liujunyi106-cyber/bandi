/**
 * 功能：附件 chips（Composer 草稿 / 用户消息内展示——Day137b-2）。
 * 三态：uploading（spinner）/ done / error（红字）；图片直接渲染缩略图，其余显示文件卡。
 */
import { FileText, Loader2, X } from "lucide-react";

import { useT } from "@/i18n";
import { formatSize, isImageAttachment } from "@/lib/attachments";
import { cn } from "@/lib/utils";
import { companyApi } from "@/services/company";
import type { ChatAttachment, PendingAttachment } from "@/stores/chat-store";

type ChipItem = ChatAttachment | PendingAttachment;

function statusOf(item: ChipItem): PendingAttachment["status"] {
  return "status" in item ? item.status : "done";
}

interface AttachmentChipsProps {
  attachments: ChipItem[];
  onRemove?: (uploadId: string) => void;
  /** v5：点击已上传附件 → 右栏打开预览页签。 */
  onOpen?: (uploadId: string, name: string) => void;
  /** 图片预览尺寸：草稿用小图，消息内用大图。 */
  imageSize?: "sm" | "md";
  className?: string;
}

export function AttachmentChips({
  attachments,
  onRemove,
  onOpen,
  imageSize = "sm",
  className,
}: AttachmentChipsProps) {
  const t = useT();
  if (!attachments.length) return null;
  return (
    <div className={cn("flex flex-wrap gap-1.5", className)} data-testid="attachment-chips">
      {attachments.map((item) => {
        const status = statusOf(item);
        const src = "status" in item ? (status === "done" ? item.upload_id : "") : item.upload_id;
        const imageSrc =
          status === "done" && src && isImageAttachment(item.name)
            ? companyApi.uploadUrl(src, item.project_id)
            : "";
        return (
          <span
            key={item.upload_id}
            className={cn(
              "border-chat-hairline bg-chat-bg text-chat-ink flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs",
              imageSrc && "flex-col items-start gap-1 p-1.5",
              status === "error" && "border-destructive/60",
            )}
            data-testid={`attachment-chip-${status}`}
          >
            {status === "uploading" ? (
              <Loader2 className="text-chat-muted size-3.5 animate-spin" />
            ) : imageSrc ? (
              <button
                type="button"
                data-testid={`attachment-image-${item.upload_id}`}
                className="block overflow-hidden rounded-md"
                title={t("chat.composer.attachPreview")}
                onClick={() => onOpen?.(item.upload_id, item.name)}
              >
                <img
                  src={imageSrc}
                  alt={item.name}
                  className={cn(
                    "object-cover",
                    imageSize === "md" ? "max-h-44 w-auto max-w-60" : "size-12",
                  )}
                />
              </button>
            ) : (
              <FileText className="text-chat-muted size-3.5" />
            )}
            {imageSrc ? (
              <span className={cn("flex w-full items-center gap-1", !onRemove && "hidden")}>
                <span className="max-w-40 flex-1 truncate text-[11px]" title={item.name}>
                  {item.name}
                </span>
                <span className="text-chat-muted text-[10px]">{formatSize(item.size)}</span>
                <a
                  className="text-chat-muted hover:text-chat-ink"
                  href={companyApi.uploadUrl(src, item.project_id)}
                  target="_blank"
                  rel="noreferrer"
                  title={t("chat.composer.attachDownload")}
                >
                  ↓
                </a>
                {onRemove ? (
                  <button
                    type="button"
                    className="text-chat-muted hover:text-chat-ink"
                    title={t("chat.composer.attachRemove")}
                    onClick={() => onRemove(item.upload_id)}
                  >
                    <X className="size-3" />
                  </button>
                ) : null}
              </span>
            ) : (
              <>
                {status === "done" && onOpen && src ? (
                  <button
                    type="button"
                    data-testid={`attachment-open-${item.upload_id}`}
                    className="max-w-40 truncate hover:underline"
                    title={t("chat.composer.attachPreview")}
                    onClick={() => onOpen(item.upload_id, item.name)}
                  >
                    {item.name}
                  </button>
                ) : (
                  <span className="max-w-40 truncate">{item.name}</span>
                )}
                <span className="text-chat-muted">{formatSize(item.size)}</span>
                {status === "error" ? (
                  <span className="text-destructive">{t("chat.composer.attachFailed")}</span>
                ) : null}
                {status === "done" && src ? (
                  <a
                    className="text-chat-muted hover:text-chat-ink"
                    href={companyApi.uploadUrl(src, item.project_id)}
                    target="_blank"
                    rel="noreferrer"
                    title={t("chat.composer.attachDownload")}
                  >
                    ↓
                  </a>
                ) : null}
                {onRemove ? (
                  <button
                    type="button"
                    className="text-chat-muted hover:text-chat-ink"
                    title={t("chat.composer.attachRemove")}
                    onClick={() => onRemove(item.upload_id)}
                  >
                    <X className="size-3" />
                  </button>
                ) : null}
              </>
            )}
          </span>
        );
      })}
    </div>
  );
}
