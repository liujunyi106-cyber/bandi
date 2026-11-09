/**
 * 功能：项目改名对话框。打开即全选、回车提交、Esc 关闭；空名禁用提交（与后端 400 一致）。
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
import { companyApi, type ProjectRecord } from "@/services/company";

interface RenameProjectDialogProps {
  /** 目标项目；null = 关闭态 */
  project: ProjectRecord | null;
  /** 改名成功后回传最新项目，供列表就地更新 */
  onRenamed: (project: ProjectRecord) => void;
  onOpenChange: (open: boolean) => void;
}

/** 功能：提交改名——PATCH 项目名成功后回调最新记录。 */
export function RenameProjectDialog({
  project,
  onRenamed,
  onOpenChange,
}: RenameProjectDialogProps) {
  const t = useT();
  const [value, setValue] = useState("");

  useEffect(() => {
    setValue(project?.name ?? "");
  }, [project]);

  const submit = async () => {
    if (!project) return;
    const name = value.trim();
    if (!name) return;
    try {
      const result = await companyApi.renameProject(project.project_id, name);
      onRenamed(result.project);
      toast.success(t("chat.project.renamed"));
      onOpenChange(false);
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : String(error));
    }
  };

  return (
    <Dialog open={project !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{t("chat.project.rename")}</DialogTitle>
        </DialogHeader>
        <Input
          value={value}
          autoFocus
          onFocus={(event) => event.currentTarget.select()}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void submit();
          }}
        />
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button disabled={!value.trim()} onClick={() => void submit()}>
            {t("common.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}