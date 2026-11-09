/**
 * 功能：「新建项目空间」弹窗。
 * 文案与行为按用户口述逐字落地：标题「新建项目空间」+ 说明「为工作空间命名，本地将自动
 * 创建同名文件夹，命名后不可随意更改」+ 输入框；右下角 = 取消 / 确定。
 * 该路径不以桌面文件作为项目空间（以桌面文件夹为项目空间走「打开本地文件夹」）。
 */
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";

interface NewProjectSpaceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 提交（返回 true 表示已成功，弹窗由调用方决定关闭时机）。 */
  onSubmit: (name: string) => Promise<boolean>;
}

export function NewProjectSpaceDialog({
  open,
  onOpenChange,
  onSubmit,
}: NewProjectSpaceDialogProps) {
  const t = useT();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const value = name.trim();
    if (!value || busy) return;
    setBusy(true);
    try {
      if (await onSubmit(value)) setName("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setName("");
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-sm" data-testid="new-project-space-dialog">
        <DialogHeader>
          <DialogTitle>{t("chat.projectSpace.createTitle")}</DialogTitle>
          <DialogDescription>{t("chat.projectSpace.createHint")}</DialogDescription>
        </DialogHeader>
        <Input
          autoFocus
          value={name}
          data-testid="new-project-space-name"
          placeholder={t("chat.projectSpace.namePlaceholder")}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void submit();
          }}
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            data-testid="new-project-space-confirm"
            disabled={!name.trim() || busy}
            onClick={() => void submit()}
          >
            {t("common.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
