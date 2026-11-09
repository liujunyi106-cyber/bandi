/**
 * 功能：桌面控制常驻提示条——启用期间在每页顶部显示「正在控制你的电脑」并支持一键关闭。
 * 状态来源：/computer/status；设置页开关后通过 window 自定义事件即时同步。
 */
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { settingsApi } from "@/services/settings";

export const COMPUTER_USE_CHANGED_EVENT = "bandi:computer-use-changed";

export function ComputerControlBanner() {
  const t = useT();
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const status = await settingsApi.computerStatus();
      setEnabled(Boolean(status.enabled));
    } catch {
      setEnabled(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const onChanged = (event: Event) => {
      const detail = (event as CustomEvent<{ enabled?: boolean }>).detail;
      if (typeof detail?.enabled === "boolean") {
        setEnabled(detail.enabled);
        return;
      }
      void load();
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    window.addEventListener(COMPUTER_USE_CHANGED_EVENT, onChanged);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener(COMPUTER_USE_CHANGED_EVENT, onChanged);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  const disable = async () => {
    setBusy(true);
    try {
      await settingsApi.setComputerEnabled(false, false);
      setEnabled(false);
      window.dispatchEvent(
        new CustomEvent(COMPUTER_USE_CHANGED_EVENT, { detail: { enabled: false } }),
      );
    } finally {
      setBusy(false);
    }
  };

  if (!enabled) return null;

  return (
    <div
      data-testid="computer-use-banner"
      className="flex h-7 shrink-0 items-center justify-center gap-2 border-b border-[#FAC775] bg-[#FAEEDA] text-[11px] text-[#854F0B]"
    >
      <span className="font-medium">{t("settingsExt.cu.banner")}</span>
      <Button
        size="sm"
        variant="ghost"
        data-testid="computer-use-banner-disable"
        className="h-5 px-1.5 text-[11px]"
        disabled={busy}
        onClick={() => void disable()}
      >
        {t("settingsExt.cu.disableNow")}
      </Button>
    </div>
  );
}
