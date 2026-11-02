/**
  * 功能：右栏详情视图（v5，@base docs/前端设计规划.md）——联动选中详情
 * 过程块工具节点 → 工具名/状态/耗时/输入/输出（错误含失败原因）。
 * 未选中时为空态提示。数据源 = chat-store 消息过程节点（不复制数据，防陈旧）。
 */
import { useEffect, useMemo } from "react";

import { formatDurationMs } from "@/components/chat/process-display";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { useChatStore, type ProcessToolNode } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

const STATUS_TONE: Record<ProcessToolNode["status"], string> = {
  running: "text-primary",
  success: "text-chat-muted",
  error: "text-destructive",
  skipped: "text-chat-muted/60",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-0.5">
      <p className="text-chat-muted text-[10px] font-medium">{label}</p>
      {children}
    </div>
  );
}

export function DetailsView({ sessionId }: { sessionId: string }) {
  const t = useT();
  const toolFocus = usePanelStore((state) => state.toolFocus);
  const messages = useChatStore((state) => state.messagesBySession[sessionId]);
  const node = useMemo(() => {
    if (!toolFocus) return null;
    const message = messages?.find((item) => item.id === toolFocus.messageId);
    const found = message?.process?.find((item) => item.id === toolFocus.nodeId);
    return found && found.kind === "tool" ? found : null;
  }, [messages, toolFocus]);

  // 工具节点被新轮次/切会话清掉 → 清空联动（防指向幽灵）
  const clearFocus = usePanelStore((state) => state.clearToolFocus);
  useEffect(() => {
    if (toolFocus && !node) clearFocus();
  }, [toolFocus, node, clearFocus]);

  if (!node) {
    return (
      <p className="text-chat-muted px-3 py-3 text-xs" data-testid="panel-details-empty">
        {t("chat.panel.detailsEmpty")}
      </p>
    );
  }

  const inputText = (() => {
    try {
      return JSON.stringify(node.input ?? {}, null, 2);
    } catch {
      return String(node.input ?? "");
    }
  })();

  return (
    <div className="space-y-2.5 px-3 py-2.5" data-testid="panel-details">
      <Field label={t("chat.panel.detailTool")}>
        <p className="text-chat-ink font-mono text-xs">{node.toolName}</p>
      </Field>
      <Field label={t("chat.panel.detailStatus")}>
        <p className={cn("text-xs font-medium", STATUS_TONE[node.status])}>
          {t(`chat.panel.toolStatus.${node.status}`)}
        </p>
      </Field>
      {typeof node.durationMs === "number" ? (
        <Field label={t("chat.panel.detailDuration")}>
          <p className="text-chat-ink text-xs tabular-nums">{formatDurationMs(node.durationMs)}</p>
        </Field>
      ) : null}
      <Field label={t("chat.panel.detailInput")}>
        <pre className="border-chat-hairline bg-chat-rail/40 text-chat-ink max-h-56 overflow-auto rounded-md border p-2 font-mono text-[11px] whitespace-pre-wrap">
          {inputText}
        </pre>
      </Field>
      {node.detail ? (
        <Field label={t("chat.panel.detailOutput")}>
          <pre
            className={cn(
              "border-chat-hairline max-h-72 overflow-auto rounded-md border p-2 font-mono text-[11px] whitespace-pre-wrap",
              node.status === "error" ? "text-destructive" : "text-chat-ink",
            )}
          >
            {node.detail}
          </pre>
        </Field>
      ) : null}
    </div>
  );
}
