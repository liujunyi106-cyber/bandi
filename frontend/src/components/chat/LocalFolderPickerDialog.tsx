/**
 * 功能：本地文件夹选择器（「打开本地文件夹」——把本机已有文件夹登记为项目空间）。
 * 数据面：GET /api/company/fs/directories（仅目录名，敏感路径后端 fail-closed）。
 * 交互：空路径 = 盘符（Windows）/ 主目录 → 逐级进入 → 底部「使用此文件夹」确认。
 * ：与「新建项目空间」并列的第二条路径（此路径以桌面/本机文件夹为项目空间）。
 */
import { ChevronUp, Folder, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

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
import { isPickableFolder } from "@/lib/project-space";
import { ApiError } from "@/services/api";
import { companyApi, type DirectoryListing } from "@/services/company";

interface LocalFolderPickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 确认选择：传入绝对路径（新建项目并设为其工作目录）。 */
  onPick: (path: string) => Promise<void> | void;
}

const EMPTY_LISTING: DirectoryListing = { path: "", parent: "", entries: [], is_root: false };

export function LocalFolderPickerDialog({
  open,
  onOpenChange,
  onPick,
}: LocalFolderPickerDialogProps) {
  const t = useT();
  const [listing, setListing] = useState<DirectoryListing>(EMPTY_LISTING);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback((path: string) => {
    setLoading(true);
    setError("");
    companyApi
      .directories(path)
      .then((result) => setListing(result))
      .catch((cause) => {
        setError(cause instanceof ApiError ? cause.message : String(cause));
        setListing(EMPTY_LISTING);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (open) load("");
  }, [open, load]);

  const confirm = async () => {
    const path = listing.path;
    if (!isPickableFolder(path)) return;
    setBusy(true);
    try {
      await onPick(path);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md" data-testid="local-folder-dialog">
        <DialogHeader>
          <DialogTitle>{t("chat.projectSpace.pickerTitle")}</DialogTitle>
          <DialogDescription>{t("chat.projectSpace.pickerHint")}</DialogDescription>
        </DialogHeader>

        <div className="flex min-w-0 items-center gap-1.5">
          <Button
            variant="outline"
            size="icon-sm"
            aria-label={t("chat.projectSpace.up")}
            data-testid="local-folder-up"
            disabled={!listing.parent || loading}
            onClick={() => load(listing.parent)}
          >
            <ChevronUp />
          </Button>
          <span
            className="text-chat-ink min-w-0 flex-1 truncate font-mono text-[11px]"
            title={listing.path || t("chat.projectSpace.thisPC")}
            data-testid="local-folder-path"
          >
            {listing.path || t("chat.projectSpace.thisPC")}
          </span>
        </div>

        <div
          className="border-chat-hairline max-h-64 min-h-40 overflow-y-auto rounded-lg border p-0.5"
          data-testid="local-folder-list"
        >
          {loading ? (
            <div className="text-chat-muted flex h-32 items-center justify-center">
              <Loader2 className="size-4 animate-spin" />
            </div>
          ) : listing.entries.length === 0 ? (
            <p className="text-chat-muted px-2 py-10 text-center text-xs">
              {error || t("chat.projectSpace.pickerEmpty")}
            </p>
          ) : (
            listing.entries.map((entry) => (
              <button
                key={entry.path}
                type="button"
                data-testid="local-folder-entry"
                className="hover:bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs"
                onClick={() => load(entry.path)}
              >
                <Folder className="text-chat-muted size-3.5 shrink-0" />
                <span className="truncate">{entry.name}</span>
              </button>
            ))
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            data-testid="local-folder-confirm"
            disabled={!listing.path || busy || loading}
            onClick={() => void confirm()}
          >
            {t("chat.projectSpace.pickerChoose")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
