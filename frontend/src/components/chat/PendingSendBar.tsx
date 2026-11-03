/**
 * 功能：执行中待发送条目（输入框上方）——复用决策卡浅色底，右侧三操作。
 * 文本条目必须点“立即发送”；带附件条目在本轮结束后自动发送。
 */
import { Paperclip, Pencil, Send, Trash2 } from "lucide-react";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PendingSend } from "@/stores/chat-store";

interface PendingSendBarProps {
  items: PendingSend[];
  executing: boolean;
  onSendNow: (item: PendingSend) => void;
  onEdit: (item: PendingSend) => void;
  onDelete: (item: PendingSend) => void;
}

const ACTION =
  "flex size-6 shrink-0 items-center justify-center rounded-md text-chat-muted transition-colors hover:bg-primary/10 hover:text-foreground disabled:pointer-events-none disabled:opacity-40";

export function PendingSendBar({
  items,
  executing,
  onSendNow,
  onEdit,
  onDelete,
}: PendingSendBarProps) {
  const t = useT();
  if (items.length === 0) return null;
  return (
    <div className="flex flex-col gap-1" data-testid="pending-send-bar">
      {items.map((item) => {
        const hasAttachments = item.attachments.length > 0;
        return (
          <div
            key={item.id}
            data-testid="pending-send-item"
            className="bg-chat-rail/60 flex items-center gap-2 rounded-xl px-3 py-2"
          >
            <span className="text-chat-muted shrink-0 text-[10px] font-medium">
              {t("chat.pendingSend.label")}
            </span>
            <span
              data-testid="pending-send-text"
              className={cn(
                "text-chat-ink min-w-0 flex-1 truncate text-xs",
                !item.text && "text-chat-muted",
              )}
            >
              {item.text || t("chat.pendingSend.attachmentOnly")}
            </span>
            {hasAttachments ? (
              <span className="text-chat-muted flex shrink-0 items-center gap-0.5 text-[10px]">
                <Paperclip className="size-3" />
                {item.attachments.length}
              </span>
            ) : null}
            <button
              type="button"
              data-testid="pending-send-now"
              className={ACTION}
              aria-label={t("chat.pendingSend.sendNow")}
              title={
                executing && hasAttachments
                  ? t("chat.pendingSend.attachmentWait")
                  : t("chat.pendingSend.sendNow")
              }
              disabled={executing && hasAttachments}
              onClick={() => onSendNow(item)}
            >
              <Send className="size-3.5" />
            </button>
            <button
              type="button"
              data-testid="pending-send-edit"
              className={ACTION}
              aria-label={t("chat.pendingSend.edit")}
              title={t("chat.pendingSend.edit")}
              onClick={() => onEdit(item)}
            >
              <Pencil className="size-3.5" />
            </button>
            <button
              type="button"
              data-testid="pending-send-delete"
              className={cn(ACTION, "hover:text-destructive")}
              aria-label={t("chat.pendingSend.delete")}
              title={t("chat.pendingSend.delete")}
              onClick={() => onDelete(item)}
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
