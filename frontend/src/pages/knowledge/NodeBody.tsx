/**
 * 功能：资料库详情渲染——文件夹概览、docx/html/pdf 预览、链接卡与外链兜底。
 * 安全：html 用 srcDoc + sandbox（无 same-origin）；链接 iframe 禁 top 导航。
 */
import { ArrowUpRight, Copy, Download, File, Folder, Link2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import type { KnowledgeNode, KnowledgeNodeContent } from "@/services/knowledge-nodes";

const HTML_CSP =
  "default-src 'none'; img-src data: blob: https: http:; media-src data: blob: https: http:; " +
  "style-src 'unsafe-inline'; font-src data: https: http:; " +
  "script-src 'unsafe-inline' 'unsafe-eval'; connect-src 'none'; form-action 'none'; " +
  "base-uri 'none'";

/** 功能：给上传的 html 注入 CSP——禁止外链请求与表单，脚本仍可内联运行。 */
function withCsp(html: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${HTML_CSP}">`;
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head[^>]*>/i, (match) => `${match}${meta}`);
  }
  if (/<html[^>]*>/i.test(html)) {
    return html.replace(/<html[^>]*>/i, (match) => `${match}<head>${meta}</head>`);
  }
  return `<head>${meta}</head>${html}`;
}

export function NodeBody({
  node,
  content,
  items,
  onOpenChild,
  onSnapshot,
  snapshotting = false,
}: {
  node: KnowledgeNode;
  content: KnowledgeNodeContent | null;
  items: KnowledgeNode[];
  onOpenChild: (node: KnowledgeNode) => void;
  onSnapshot?: () => void;
  snapshotting?: boolean;
}) {
  const t = useT();
  if (node.kind === "folder") {
    return (
      <div className="p-6">
        {items.length === 0 ? (
          <p className="text-chat-muted text-xs">{t("knowledge.folderHint")}</p>
        ) : (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
            {items.map((child) => (
              <button
                className="border-chat-hairline hover:bg-primary/5 flex min-h-16 items-center gap-2 rounded-xl border px-3 py-2 text-left"
                key={child.id}
                onClick={() => onOpenChild(child)}
                type="button"
              >
                {child.kind === "folder" ? (
                  <Folder className="size-4 shrink-0" />
                ) : (
                  <File className="size-4 shrink-0" />
                )}
                <span className="min-w-0 flex-1 truncate text-xs">{child.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>
    );
  }
  if (!content) {
    return <p className="text-chat-muted p-4 text-xs">{t("knowledge.loading")}</p>;
  }
  if (node.kind === "docx" && content.html) {
    return (
      <iframe
        className="h-full min-h-[520px] w-full bg-white"
        sandbox="allow-scripts"
        srcDoc={content.html}
        title={node.name}
      />
    );
  }
  if (node.kind === "html") {
    return (
      <iframe
        className="h-full min-h-[420px] w-full bg-white"
        sandbox="allow-scripts"
        srcDoc={withCsp(content.html ?? "")}
        title={node.name}
      />
    );
  }
  if (node.kind === "link") {
    return (
      <LinkPreview
        name={node.name}
        onSnapshot={onSnapshot}
        snapshotAt={content.snapshot_at}
        snapshotHtml={content.snapshot_html}
        snapshotting={snapshotting}
        url={content.url ?? ""}
      />
    );
  }
  if (content.pdf_url) {
    return (
      <iframe
        className="h-full min-h-[640px] w-full"
        src={content.pdf_url}
        title={node.name}
      />
    );
  }
  return (
    <div className="p-6">
      <p className="text-chat-muted mb-3 text-xs">{t("knowledge.previewFailed")}</p>
      <Button asChild size="sm" variant="outline">
        <a download href={`/api/knowledge/nodes/${node.id}/download`}>
          <Download className="size-3.5" />
          {t("knowledge.download")}
        </a>
      </Button>
    </div>
  );
}

function LinkPreview({
  name,
  url,
  snapshotHtml,
  snapshotAt,
  snapshotting,
  onSnapshot,
}: {
  name: string;
  url: string;
  snapshotHtml?: string;
  snapshotAt?: number;
  snapshotting: boolean;
  onSnapshot?: () => void;
}) {
  const t = useT();
  const copy = () => void navigator.clipboard?.writeText(url);
  const safe = /^https?:\/\//i.test(url.trim());
  if (!safe) {
    return (
      <div className="p-6">
        <p className="text-chat-muted mb-2 text-xs">{t("knowledge.linkInvalid")}</p>
        <p className="text-chat-ink break-all text-xs">{url}</p>
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-chat-hairline flex items-center gap-2 border-b px-4 py-2">
        <Link2 className="text-chat-muted size-4 shrink-0" />
        <span className="text-chat-muted min-w-0 flex-1 truncate text-xs">{url}</span>
        {onSnapshot ? (
          <Button
            disabled={snapshotting}
            onClick={onSnapshot}
            size="sm"
            variant="outline"
          >
            {snapshotting
              ? t("knowledge.snapshotting")
              : snapshotHtml
                ? t("knowledge.refreshSnapshot")
                : t("knowledge.fetchSnapshot")}
          </Button>
        ) : null}
        <Button asChild size="sm" variant="ghost">
          <a href={url} rel="noreferrer noopener" target="_blank">
            <ArrowUpRight className="size-3.5" />
            {t("knowledge.openExternal")}
          </a>
        </Button>
        <Button aria-label={t("knowledge.copyLink")} onClick={copy} size="icon-sm" variant="ghost">
          <Copy className="size-3.5" />
        </Button>
      </div>
      {snapshotHtml ? (
        <>
          <iframe
            className="min-h-0 w-full flex-1 bg-white"
            referrerPolicy="no-referrer"
            sandbox=""
            srcDoc={withCsp(snapshotHtml)}
            title={name}
          />
          <p className="text-chat-muted px-4 py-2 text-[11px]">
            {t("knowledge.snapshotHint")}
            {snapshotAt ? ` · ${new Date(snapshotAt * 1000).toLocaleString()}` : ""}
          </p>
        </>
      ) : (
        <>
          <iframe
            className="min-h-0 w-full flex-1 bg-white"
            referrerPolicy="no-referrer"
            sandbox="allow-forms allow-scripts allow-same-origin"
            src={url}
            title={name}
          />
          <p className="text-chat-muted px-4 py-2 text-[11px]">{t("knowledge.linkEmbedHint")}</p>
        </>
      )}
    </div>
  );
}
