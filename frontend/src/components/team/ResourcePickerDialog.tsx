/**
 * 功能：资源选择弹窗（多选绑定 + 上传本地文件）。
 * 资源是共享的，同一个技能/SOP/知识库可绑定给多个员工。
 */
import { Upload } from "lucide-react";
import { useRef } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export interface ResourceOption {
  id: string;
  label: string;
  hint?: string;
}

export interface ResourcePickerDialogProps {
  open: boolean;
  title: string;
  items: ResourceOption[];
  selectedIds: string[];
  submitting?: boolean;
  uploadLabel?: string;
  onOpenChange: (open: boolean) => void;
  onToggle: (id: string) => void;
  onSubmit: () => void;
  onUpload: (file: File) => void | Promise<void>;
}

/** 功能：多选资源并上传本地文件。 */
export function ResourcePickerDialog({
  open,
  title,
  items,
  selectedIds,
  submitting = false,
  uploadLabel,
  onOpenChange,
  onToggle,
  onSubmit,
  onUpload,
}: ResourcePickerDialogProps) {
  const t = useT();
  const fileRef = useRef<HTMLInputElement>(null);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="text-sm">{title}</DialogTitle>
          <DialogDescription className="text-chat-muted text-[11px]">
            {t("team.resources.multiHint")}
          </DialogDescription>
        </DialogHeader>

        <div className="border-chat-hairline max-h-[300px] overflow-y-auto rounded-lg border p-1">
          {items.length === 0 ? (
            <p className="text-chat-muted px-3 py-8 text-center text-[11px]">
              {t("team.resources.noCandidates")}
            </p>
          ) : (
            items.map((item) => {
              const active = selectedIds.includes(item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => onToggle(item.id)}
                  className="hover:bg-primary/5 flex w-full items-center gap-2 rounded px-2 py-2 text-left"
                >
                  <span
                    className={cn(
                      "grid size-4 shrink-0 place-items-center rounded border text-[9px]",
                      active
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-chat-hairline",
                    )}
                  >
                    {active ? "✓" : null}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="text-chat-ink block truncate text-xs">{item.label}</span>
                    {item.hint ? (
                      <span className="text-chat-muted mt-0.5 line-clamp-1 block text-[10px]">
                        {item.hint}
                      </span>
                    ) : null}
                  </span>
                </button>
              );
            })
          )}
        </div>

        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          className="border-chat-hairline text-chat-muted hover:text-chat-ink flex w-full items-center justify-center gap-2 rounded-lg border border-dashed px-3 py-2 text-xs transition-colors"
        >
          <Upload className="size-3.5" />
          {uploadLabel ?? t("team.resources.upload")}
        </button>
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void onUpload(file);
            event.target.value = "";
          }}
        />

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("team.add.cancel")}
          </Button>
          <Button disabled={selectedIds.length === 0 || submitting} onClick={onSubmit}>
            {submitting
              ? t("team.resources.binding")
              : `${t("team.resources.bindSelected")} (${selectedIds.length})`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
