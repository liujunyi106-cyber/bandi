/**
 * 功能：会话重命名对话框（侧栏会话菜单与顶栏标题按钮共用入口， 抽取）。
 */
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { ApiError } from "@/services/api";
import { sessionsApi, type SessionSummary } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";

interface RenameSessionDialogProps {
  /** 目标会话（null = 关闭）。 */
  session: SessionSummary | null;
  onOpenChange: (open: boolean) => void;
}

/** 功能：会话重命名对话框（确认后 PATCH 落库 + 本地列表同步）。 */
export function RenameSessionDialog({ session, onOpenChange }: RenameSessionDialogProps) {
  const t = useT();
  const [value, setValue] = useState("");

  useEffect(() => {
    setValue(session?.title ?? "");
  }, [session]);

  const submit = async () => {
    if (!session) return;
    const title = value.trim();
    if (!title) return;
    try {
      await sessionsApi.rename(session.id, title);
      useChatStore.getState().upsertSession({ ...session, title });
      toast.success(t("chat.session.renamed"));
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    }
  };

  return (
    <Dialog open={session !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("chat.session.rename")}</DialogTitle>
        </DialogHeader>
        <Input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void submit();
          }}
          autoFocus
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button onClick={() => void submit()}>{t("common.confirm")}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
