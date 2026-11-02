/**
 * 功能：MCP 服务管理弹窗（引导态 → 配置态，编辑 ~/.bandi/mcp.json）。
 */
import { ArrowLeft, Server, Settings } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useT } from "@/i18n";
import { settingsApi } from "@/services/settings";

type McpMode = "intro" | "edit";

export interface McpConfigDialogProps {
  open: boolean;
  initialMode: McpMode;
  onOpenChange: (open: boolean) => void;
  onSaved: () => void;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 功能：MCP 服务管理两态弹窗。 */
export function McpConfigDialog({
  open,
  initialMode,
  onOpenChange,
  onSaved,
}: McpConfigDialogProps) {
  const t = useT();
  const [mode, setMode] = useState<McpMode>(initialMode);
  const [configPath, setConfigPath] = useState("~/.bandi/mcp.json");
  const [source, setSource] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await settingsApi.mcpConfig();
      setConfigPath(payload.path);
      setSource(`${JSON.stringify(payload.content, null, 2)}\n`);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setMode(initialMode);
    setError("");
    void load();
  }, [initialMode, load, open]);

  const save = async () => {
    let document: unknown;
    try {
      document = JSON.parse(source);
    } catch {
      setError(t("skillsMarket.mcp.invalidJson"));
      return;
    }
    if (
      !document ||
      typeof document !== "object" ||
      Array.isArray(document) ||
      !("mcpServers" in document) ||
      typeof (document as { mcpServers?: unknown }).mcpServers !== "object" ||
      Array.isArray((document as { mcpServers?: unknown }).mcpServers)
    ) {
      setError(t("skillsMarket.mcp.missingServers"));
      return;
    }
    setSaving(true);
    setError("");
    try {
      const result = await settingsApi.saveMcpConfig(
        document as { mcpServers: Record<string, Record<string, unknown>> },
      );
      toast.success(t("skillsMarket.mcp.saveOk").replace("{n}", String(result.count)));
      onSaved();
      onOpenChange(false);
    } catch (reason) {
      const message = errorText(reason);
      setError(message);
      toast.error(`${t("skillsMarket.mcp.saveFailed")}：${message}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="gap-0 p-0 sm:max-w-[720px]">
        <DialogHeader className="gap-0 p-4 pr-12">
          <div className="flex items-start gap-3">
            <span className="bg-primary text-primary-foreground grid size-9 shrink-0 place-items-center rounded-[10px]">
              <Server className="size-[18px]" />
            </span>
            <div className="min-w-0">
              <DialogTitle className="text-base font-medium">{t("skillsMarket.mcp.title")}</DialogTitle>
              <DialogDescription className="text-muted-foreground mt-1 text-[13px]">
                {t("skillsMarket.mcp.subtitle")}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <div className="border-chat-hairline border-t" />

        {mode === "intro" ? (
          <div className="flex flex-col items-center justify-center gap-3 px-6 py-11 text-center">
            <span className="bg-primary/5 text-chat-muted grid size-16 place-items-center rounded-2xl">
              <Server className="size-8" />
            </span>
            <strong className="text-chat-ink text-sm font-semibold">
              {t("skillsMarket.mcp.introTitle")}
            </strong>
            <p className="text-chat-muted max-w-[380px] text-xs leading-5">
              {t("skillsMarket.mcp.introHint")}
            </p>
            <Button
              className="mt-1"
              disabled={loading}
              onClick={() => setMode("edit")}
            >
              <Settings />
              {t("skillsMarket.mcp.configure")}
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-3 p-4 pt-3">
            <div className="flex items-center justify-between gap-3">
              <Button
                variant="ghost"
                className="text-chat-muted -ml-2"
                onClick={() => setMode("intro")}
              >
                <ArrowLeft />
                {t("skillsMarket.mcp.back")}
              </Button>
              <div className="flex items-center gap-2">
                <Button variant="outline" onClick={() => onOpenChange(false)}>
                  {t("common.cancel")}
                </Button>
                <Button disabled={saving || loading} onClick={() => void save()}>
                  {saving ? t("skillsMarket.saving") : t("skillsMarket.mcp.save")}
                </Button>
              </div>
            </div>

            <div className="border-chat-hairline bg-chat-bg flex items-center gap-2 rounded-lg border px-3 py-2 text-[11px]">
              <span className="text-chat-muted shrink-0">{t("skillsMarket.mcp.pathLabel")}</span>
              <code className="text-chat-ink min-w-0 truncate font-mono text-[11px]">{configPath}</code>
            </div>

            <textarea
              value={source}
              spellCheck={false}
              disabled={loading}
              aria-label={t("skillsMarket.mcp.sourceLabel")}
              onChange={(event) => {
                setSource(event.target.value);
                setError("");
              }}
              className={`h-[320px] w-full resize-none rounded-lg border bg-chat-bg px-3 py-3 font-mono text-xs leading-6 outline-none focus:bg-card ${
                error ? "border-destructive" : "border-chat-hairline"
              }`}
            />

            <div className="text-chat-muted flex items-start justify-between gap-3 text-[11px]">
              <span className={error ? "text-destructive" : ""}>
                {error || t("skillsMarket.mcp.hint")}
              </span>
              <span className="text-muted-foreground-weak shrink-0 font-mono">
                {"{"}"mcpServers": {"{}"}
                {"}"}
              </span>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
