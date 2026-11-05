/**
 * 功能：右栏页签内容预览（v5，@base docs/前端设计规划.md §8）——文件 / 制品 / 对话附件三源统一：
 * 文本类：markdown 渲染（.md）或带行号纯文本；二进制 / 超大：元数据卡（后端 fail-closed 不回内容）；
 * 附件图片：objectURL 直显。预览只读。
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import { useT } from "@/i18n";
import { formatSize } from "@/lib/attachments";
import { ApiError } from "@/services/api";
import { panelApi } from "@/services/panel";
import { sessionsApi } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";
import type { PanelTab } from "@/stores/panel-store";

/** 功能：文本预览上限（字符；超出截断提示——后端条目上限 64KB，附件无上限需本地截断）。 */
const MAX_PREVIEW_CHARS = 65536;
/** 功能：行号渲染行数上限（防超大文本 DOM 膨胀）。 */
const MAX_NUMBERED_LINES = 5000;

type PreviewState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "text"; content: string; truncated: boolean }
  | { status: "meta"; size: number; reason: "binary" | "too_large" }
  | { status: "image"; url: string; size: number };

/** 功能：markdown 扩展名判定（预览渲染方式选择）。 */
function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown)$/i.test(path);
}

/** 功能：附件可读文本 MIME 判定（其余按二进制元数据卡）。 */
function isTextMime(mime: string): boolean {
  return (
    mime.startsWith("text/") ||
    mime.includes("json") ||
    mime.includes("xml") ||
    mime.includes("yaml") ||
    mime.includes("csv")
  );
}

/** 功能：路径归一（反斜杠 → 正斜杠；工具输入路径可能为绝对/相对）。 */
function normalizePath(value: string): string {
  return value.replace(/\\/g, "/");
}

/** 功能：工具输入路径与页签路径匹配（完全一致或后缀匹配）。 */
function pathMatches(toolPath: string, tabPath: string): boolean {
  const tool = normalizePath(toolPath);
  const tab = normalizePath(tabPath);
  return tool === tab || tool.endsWith(`/${tab}`);
}

/** 功能：带行号纯文本（行号列与正文同字体行高，滚动同步）；超行数上限截断。 */
function NumberedText({ content }: { content: string }) {
  const t = useT();
  const lines = useMemo(() => content.split("\n"), [content]);
  const shown = lines.slice(0, MAX_NUMBERED_LINES);
  const gutter = shown.map((_, index) => index + 1).join("\n");
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 overflow-auto font-mono text-[11px] leading-[1.7]">
        <pre className="text-chat-muted/60 shrink-0 border-r border-chat-hairline px-2 py-2 text-right select-none">
          {gutter}
        </pre>
        <pre className="text-chat-ink min-w-0 flex-1 px-3 py-2 whitespace-pre">
          {shown.join("\n")}
        </pre>
      </div>
      {lines.length > MAX_NUMBERED_LINES ? (
        <p className="text-chat-muted shrink-0 px-3 py-1 text-[11px]">
          {t("chat.panel.previewLineLimit").replace("{n}", String(MAX_NUMBERED_LINES))}
        </p>
      ) : null}
    </div>
  );
}

/** 功能：元数据卡（二进制 / 超大——不渲染内容，只给身份与说明）。 */
function MetaCard({ size, reason }: { size: number; reason: "binary" | "too_large" }) {
  const t = useT();
  return (
    <div className="px-3 py-3">
      <div className="border-chat-hairline bg-chat-rail/40 rounded-lg border p-3">
        <p className="text-chat-ink text-xs">
          {reason === "too_large"
            ? t("chat.panel.fileTooLarge").replace("{size}", formatSize(size))
            : t("chat.panel.fileBinary")}
        </p>
        <p className="text-chat-muted mt-1 text-[11px]">
          {t("chat.panel.previewSize")}: {formatSize(size)}
        </p>
      </div>
    </div>
  );
}

export function PanelTabContent({ sessionId, tab }: { sessionId: string; tab: PanelTab }) {
  const t = useT();
  const [state, setState] = useState<PreviewState>({ status: "loading" });
  const objectUrlRef = useRef("");
  // v5：文件实时更新——write/edit 的 tool_result 帧匹配本页签路径 → 自动刷新（mtime 由后端实时读回）
  const fileSignal = useChatStore((store) => {
    if (tab.kind !== "file" || !tab.path) return "";
    const targetPath = tab.path;
    let signature = "";
    for (const message of store.messagesBySession[sessionId] ?? []) {
      for (const node of message.process ?? []) {
        if (node.kind !== "tool" || node.status !== "success") continue;
        const inputPath = typeof node.input?.path === "string" ? node.input.path : "";
        if (inputPath && pathMatches(inputPath, targetPath)) {
          signature = `${node.toolCallId ?? node.id}:${node.status}`;
        }
      }
    }
    return signature;
  });
  const lastSignalRef = useRef(fileSignal);
  const [reloadKey, setReloadKey] = useState(0);
  useEffect(() => {
    // 帧驱动刷新：mount 基线不触发——仅在工具结果新到达（信号变化）时重拉
    if (fileSignal && fileSignal !== lastSignalRef.current) {
      setReloadKey((value) => value + 1);
    }
    lastSignalRef.current = fileSignal;
  }, [fileSignal]);

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    const fail = (error: unknown) => {
      if (cancelled) return;
      const message =
        error instanceof ApiError
          ? error.message
          : error instanceof Error
            ? error.message
            : String(error);
      setState({ status: "error", message });
    };
    if (tab.kind === "file" && tab.path) {
      // 项目文件树来源（tab.projectId）→ 走项目端点；否则会话文件根
      const request = tab.projectId
        ? panelApi.projectFileContent(tab.projectId, tab.path)
        : panelApi.fileContent(sessionId, tab.path);
      request
        .then((payload) => {
          if (cancelled) return;
          if (payload.kind === "text") {
            setState({
              status: "text",
              content: payload.content,
              truncated: Boolean(payload.truncated),
            });
          } else {
            setState({ status: "meta", size: payload.size, reason: payload.kind });
          }
        })
        .catch(fail);
    } else if (tab.kind === "artifact" && (tab.projectId || tab.path) && !tab.artifactId) {
      // 项目聚合产物（右栏「产物」来自项目扫描，id 不在会话 artifacts 内）→ 按路径读
      const request = tab.projectId
        ? panelApi.projectFileContent(tab.projectId, tab.path ?? "")
        : panelApi.fileContent(sessionId, tab.path ?? "");
      request
        .then((payload) => {
          if (cancelled) return;
          if (payload.kind === "text") {
            setState({
              status: "text",
              content: payload.content,
              truncated: Boolean(payload.truncated),
            });
          } else {
            setState({ status: "meta", size: payload.size, reason: payload.kind });
          }
        })
        .catch(fail);
    } else if (tab.kind === "artifact" && tab.artifactId) {
      sessionsApi
        .artifactContent(sessionId, tab.artifactId)
        .then((payload) => {
          if (cancelled) return;
          if (payload.kind === "text") {
            setState({
              status: "text",
              content: payload.content,
              truncated: Boolean(payload.truncated),
            });
          } else {
            setState({ status: "meta", size: payload.size, reason: payload.kind });
          }
        })
        .catch(fail);
    } else if (tab.kind === "attachment" && tab.uploadId) {
      fetch(`/api/company/uploads/${encodeURIComponent(tab.uploadId)}`)
        .then(async (response) => {
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const mime = response.headers.get("content-type") ?? "";
          if (mime.startsWith("image/")) {
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            if (cancelled) {
              URL.revokeObjectURL(url);
              return;
            }
            objectUrlRef.current = url;
            setState({ status: "image", url, size: blob.size });
            return;
          }
          if (isTextMime(mime)) {
            const text = await response.text();
            setState({
              status: "text",
              content: text.slice(0, MAX_PREVIEW_CHARS),
              truncated: text.length > MAX_PREVIEW_CHARS,
            });
            return;
          }
          setState({
            status: "meta",
            size: Number(response.headers.get("content-length") ?? 0),
            reason: "binary",
          });
        })
        .catch(fail);
    } else {
      setState({ status: "error", message: t("chat.panel.previewFailed") });
    }
    return () => {
      cancelled = true;
      if (objectUrlRef.current) {
        URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = "";
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, tab.id, reloadKey]);

  const path = tab.path ?? tab.title;

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="panel-preview">
      <div className="border-chat-hairline text-chat-muted flex shrink-0 items-center gap-2 border-b px-3 py-1.5 text-[11px]">
        <span className="min-w-0 flex-1 truncate font-mono" title={path}>
          {path}
        </span>
      </div>
      {state.status === "loading" ? (
        <p className="text-chat-muted px-3 py-3 text-xs">{t("chat.panel.previewLoading")}</p>
      ) : null}
      {state.status === "error" ? (
        <p className="text-destructive px-3 py-3 text-xs" data-testid="panel-preview-error">
          {state.message || t("chat.panel.previewFailed")}
        </p>
      ) : null}
      {state.status === "text" ? (
        <>
          {isMarkdownPath(path) ? (
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
              <MarkdownRenderer text={state.content} />
            </div>
          ) : (
            <NumberedText content={state.content} />
          )}
          {state.truncated ? (
            <p className="text-chat-muted shrink-0 px-3 py-1 text-[11px]">
              {t("chat.panel.artifactTruncated")}
            </p>
          ) : null}
        </>
      ) : null}
      {state.status === "meta" ? <MetaCard size={state.size} reason={state.reason} /> : null}
      {state.status === "image" ? (
        <div className="min-h-0 flex-1 overflow-auto px-3 py-2">
          <img
            src={state.url}
            alt={tab.title}
            data-testid="panel-preview-image"
            className="border-chat-hairline max-w-full rounded-lg border"
          />
        </div>
      ) : null}
    </div>
  );
}
