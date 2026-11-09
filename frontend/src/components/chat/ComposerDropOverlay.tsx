/**
 * 功能：Composer 拖拽覆盖层。
 * 视觉：卡片 + 主色虚线描边（不用毛玻璃）。
 */
import { Upload } from "lucide-react";

import { useT } from "@/i18n";

export function ComposerDropOverlay() {
  const t = useT();
  return (
    <div
      data-testid="composer-drop-overlay"
      className="border-primary/50 bg-card/85 pointer-events-none absolute inset-0 z-40 flex items-center justify-center rounded-xl border-2 border-dashed"
    >
      <div className="flex flex-col items-center gap-1">
        <Upload className="text-primary size-6" />
        <p className="text-chat-ink text-sm font-medium">{t("chat.composer.dropTitle")}</p>
        <p className="text-chat-muted text-xs">{t("chat.composer.dropHint")}</p>
      </div>
    </div>
  );
}
