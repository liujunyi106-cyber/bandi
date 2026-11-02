/**
 * 功能：xlsx 预览——工作表页签 + 只读网格；读取失败时降级到 PDF 预览。
 * 数据源：GET /api/knowledge/nodes/{id}/sheets 与 /sheets/{name}。
 */
import { useEffect, useState } from "react";

import { useT } from "@/i18n";
import { knowledgeNodesApi } from "@/services/knowledge-nodes";

export function XlsxView({ nodeId }: { nodeId: string }) {
  const t = useT();
  const [sheets, setSheets] = useState<string[]>([]);
  const [active, setActive] = useState("");
  const [rows, setRows] = useState<string[][]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    knowledgeNodesApi
      .listSheets(nodeId)
      .then((payload) => {
        if (cancelled) return;
        setSheets(payload.sheets ?? []);
        setActive(payload.sheets?.[0] ?? "");
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    knowledgeNodesApi
      .sheetRows(nodeId, active)
      .then((payload) => {
        if (!cancelled) setRows(payload.rows ?? []);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [active, nodeId]);

  if (error) {
    return (
      <div className="flex h-full flex-col">
        <p className="text-destructive px-4 py-2 text-xs">{`${t("knowledge.loadFailed")}：${error}`}</p>
        <div className="px-4 pb-2">
          <a
            className="text-chat-ink text-xs underline"
            download
            href={`/api/knowledge/nodes/${nodeId}/download`}
          >
            {t("knowledge.download")}
          </a>
        </div>
        <iframe
          className="min-h-0 w-full flex-1"
          src={`/api/knowledge/nodes/${nodeId}/preview`}
          title={t("knowledge.tabDocs")}
        />
      </div>
    );
  }

  const width = Math.max(0, ...rows.map((row) => row.length));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        aria-label={t("knowledge.sheetTabs")}
        className="flex flex-wrap items-center gap-1 border-b border-[var(--divider)] px-2 py-1.5"
        role="tablist"
      >
        {sheets.map((sheet) => (
          <button
            aria-selected={sheet === active}
            className={`rounded-md px-2 py-1 text-xs ${
              sheet === active
                ? "bg-primary/5 text-chat-ink font-medium"
                : "text-chat-muted hover:bg-primary/5"
            }`}
            key={sheet}
            onClick={() => setActive(sheet)}
            role="tab"
            type="button"
          >
            {sheet}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-xs">
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr className="border-b border-[var(--divider)]" key={rowIndex}>
                {Array.from({ length: width }, (_, colIndex) => (
                  <td
                    className={`max-w-[240px] truncate px-2 py-1 ${
                      rowIndex === 0 ? "bg-primary/5 font-medium" : ""
                    }`}
                    key={colIndex}
                  >
                    {row[colIndex] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 ? (
          <p className="text-chat-muted p-6 text-center text-xs">{t("knowledge.loading")}</p>
        ) : null}
      </div>
    </div>
  );
}
