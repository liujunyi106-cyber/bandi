/**
  * 功能：附件 chips。
 * 三态：uploading（spinner）/ done / error（红字）；可选移除按钮。
 */
import { FileText, Loader2, X } from "lucide-react";

import { useT } from "@/i18n";
import { formatSize } from "@/lib/attachments";
import { cn } from "@/lib/utils";
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
  className?: string;
}

export function AttachmentChips({
  attachments,
  onRemove,
  onOpen,
  className,
}: AttachmentChipsProps) {
  const t = useT();
  if (!attachments.length) return null;
  return (
    <div className={cn("flex flex-wrap gap-1.5", className)} data-testid="attachment-chips">
      {attachments.map((item) => {
        const status = statusOf(item);
        const src = "status" in item ? (status === "done" ? item.upload_id : "") : item.upload_id;
        return (
          <span
            key={item.upload_id}
            className={cn(
              "border-chat-hairline bg-chat-bg text-chat-ink flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs",
              status === "error" && "border-destructive/60",
            )}
            data-testid={`attachment-chip-${status}`}
          >
            {status === "uploading" ? (
              <Loader2 className="text-chat-muted size-3.5 animate-spin" />
            ) : (
              <FileText className="text-chat-muted size-3.5" />
            )}
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
                href={`/api/company/uploads/${encodeURIComponent(src)}`}
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
          </span>
        );
      })}
    </div>
  );
}
