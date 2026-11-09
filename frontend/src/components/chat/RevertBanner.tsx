/**
 * 功能：回滚暂存条。
 * revert 三级：stage 由消息菜单触发（本组件展示）；commit = 确认截断（危险）；
 * clear = 撤销暂存。仅在当前会话有暂存时渲染。
 */
import { RotateCcw, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

interface RevertBannerProps {
  entryId: string;
  onCommit: () => void;
  onClear: () => void;
}

export function RevertBanner({ entryId, onCommit, onClear }: RevertBannerProps) {
  const t = useT();
  return (
    <div
      className="border-warning/40 bg-warning/10 text-warning mx-4 mt-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs"
      data-testid="revert-banner"
    >
      <RotateCcw className="size-3.5 shrink-0" />
      <span className="flex-1">{t("chat.revert.staged").replace("{id}", entryId.slice(0, 8))}</span>
      <Button variant="outline" size="sm" className="h-6 px-2 text-xs" onClick={onClear}>
        <X className="size-3" /> {t("chat.revert.clear")}
      </Button>
      <Button variant="destructive" size="sm" className="h-6 px-2 text-xs" onClick={onCommit}>
        {t("chat.revert.commit")}
      </Button>
    </div>
  );
}
