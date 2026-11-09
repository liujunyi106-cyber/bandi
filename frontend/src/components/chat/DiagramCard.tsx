/**
 * 功能：图表卡片（mermaid/SVG 渲染）——左上角「…」菜单：
 * 查看代码 ↔ 显示 UI（icon 随之切换）、复制代码、下载到本地（源文件 .mmd）
 * 下载为图片（PNG）。流式期间保留上一版 SVG 防闪烁；失败/加载中是代码回退。
 */
import { Code, Copy, Download, Eye, ImageDown, MoreHorizontal } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { toast } from "sonner";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

const MERMAID_RENDER_DEBOUNCE_MS = 200;

let mermaidLoader: Promise<typeof import("mermaid").default> | null = null;

function loadMermaid(): Promise<typeof import("mermaid").default> {
  if (!mermaidLoader) {
    mermaidLoader = import("mermaid").then((module) => {
      const mermaid = module.default;
      mermaid.initialize({
        startOnLoad: false,
        theme: "base",
        securityLevel: "strict",
        fontFamily: "inherit",
        themeVariables: {
          primaryColor: "#eef2ff",
          primaryBorderColor: "#c7d2fe",
          primaryTextColor: "#1e1b4b",
          lineColor: "#94a3b8",
          secondaryColor: "#f8fafc",
          tertiaryColor: "#f1f5f9",
          fontSize: "14px",
        },
      });
      return mermaid;
    });
  }
  return mermaidLoader;
}

function triggerDownload(href: string, filename: string): void {
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  anchor.click();
}

export function DiagramCard({ code }: { code: string }) {
  const t = useT();
  const reactId = useId();
  const [svg, setSvg] = useState("");
  const [failed, setFailed] = useState(false);
  const [viewMode, setViewMode] = useState<"ui" | "code">("ui");

  useEffect(() => {
    let cancelled = false;
    const elementId = `mmd-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
    // 不在此处清空 svg：保留上一版渲染结果，直到新版就绪（防流式增量闪烁）
    const timer = window.setTimeout(() => {
      loadMermaid()
        .then((mermaid) => mermaid.render(elementId, code))
        .then((result) => {
          if (cancelled) return;
          setSvg(result.svg);
          setFailed(false);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    }, MERMAID_RENDER_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [code, reactId]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      toast.success(t("chat.message.diagram.copyCode"));
    } catch {
      toast.error(t("chat.message.diagram.copyCode"));
    }
  };

  const handleDownloadSource = () => {
    const blob = new Blob([code], { type: "text/plain;charset=utf-8" });
    triggerDownload(URL.createObjectURL(blob), "diagram.mmd");
  };

  const handleDownloadImage = async () => {
    try {
      const svgBlob = new Blob([svg], { type: "image/svg+xml;charset=utf-8" });
      const url = URL.createObjectURL(svgBlob);
      const image = new Image();
      await new Promise<void>((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = () => reject(new Error("svg load failed"));
        image.src = url;
      });
      const canvas = document.createElement("canvas");
      canvas.width = image.naturalWidth || 800;
      canvas.height = image.naturalHeight || 600;
      canvas.getContext("2d")?.drawImage(image, 0, 0);
      URL.revokeObjectURL(url);
      canvas.toBlob((blob) => {
        if (blob) triggerDownload(URL.createObjectURL(blob), "diagram.png");
      }, "image/png");
    } catch {
      toast.error(t("chat.message.diagram.downloadImage"));
    }
  };

  const showUi = Boolean(svg) && !failed && viewMode === "ui";

  return (
    <div
      className={cn(
        "mermaid-figure group relative my-2 overflow-hidden rounded-lg",
        showUi ? "bg-card" : "bg-chat-rail",
      )}
      data-testid="diagram-card"
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid="diagram-more"
            className="no-focus-ring text-chat-muted/70 hover:text-foreground hover:bg-chat-rail absolute right-2 top-2 z-10 flex size-6 items-center justify-center rounded-md transition-colors focus-visible:opacity-100"
            title={t("common.more")}
          >
            <MoreHorizontal className="size-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {viewMode === "code" ? (
            <DropdownMenuItem data-testid="diagram-toggle-view" onClick={() => setViewMode("ui")}>
              <Eye /> {t("chat.message.diagram.viewUi")}
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem data-testid="diagram-toggle-view" onClick={() => setViewMode("code")}>
              <Code /> {t("chat.message.diagram.viewCode")}
            </DropdownMenuItem>
          )}
          <DropdownMenuItem data-testid="diagram-copy" onClick={() => void handleCopy()}>
            <Copy /> {t("chat.message.diagram.copyCode")}
          </DropdownMenuItem>
          <DropdownMenuItem data-testid="diagram-download-source" onClick={handleDownloadSource}>
            <Download /> {t("chat.message.diagram.downloadSource")}
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="diagram-download-image"
            onClick={() => void handleDownloadImage()}
          >
            <ImageDown /> {t("chat.message.diagram.downloadImage")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {showUi ? (
        <div
          className="flex justify-center overflow-x-auto p-3 [&_svg]:h-auto [&_svg]:max-w-[440px]"
          data-testid="diagram-svg"
          dangerouslySetInnerHTML={{ __html: svg }}
        />
      ) : (
        <pre
          className="max-h-[420px] overflow-auto p-3 pt-9 text-xs [&::-webkit-scrollbar-thumb]:bg-chat-hairline [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar]:w-1.5 [scrollbar-width:thin]"
          data-testid="diagram-code"
        >
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}
