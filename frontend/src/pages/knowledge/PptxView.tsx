/**
 * 功能：pptx 预览——缩略图列表 + 逐页文本 + 上下页（支持 ←/→）。
 * 数据源：GET /api/knowledge/nodes/{id}/slides；失败降级 PDF。
 */
import { useCallback, useEffect, useState } from "react";

import { useT } from "@/i18n";
import { knowledgeNodesApi } from "@/services/knowledge-nodes";

interface SlideSummary {
  index: number;
  title: string;
  text: string;
}

export function PptxView({ nodeId }: { nodeId: string }) {
  const t = useT();
  const [slides, setSlides] = useState<SlideSummary[]>([]);
  const [active, setActive] = useState(0);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    knowledgeNodesApi
      .slides(nodeId)
      .then((payload) => {
        if (cancelled) return;
        setSlides(payload.slides ?? []);
        setActive(0);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  const move = useCallback(
    (delta: number) => {
      setActive((current) => Math.min(slides.length - 1, Math.max(0, current + delta)));
    },
    [slides.length],
  );

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

  const current = slides[active];

  return (
    <div
      className="flex h-full min-h-0"
      onKeyDown={(event) => {
        if (event.key === "ArrowRight") move(1);
        if (event.key === "ArrowLeft") move(-1);
      }}
      tabIndex={0}
    >
      <aside
        aria-label={t("knowledge.slideList")}
        className="w-44 shrink-0 overflow-y-auto border-r border-[var(--divider)] p-2"
      >
        {slides.map((slide, index) => (
          <button
            aria-current={index === active ? "true" : undefined}
            className={`mb-1 w-full rounded-lg px-2 py-2 text-left text-xs ${
              index === active
                ? "bg-primary/5 text-chat-ink font-medium"
                : "text-chat-muted hover:bg-primary/5"
            }`}
            key={slide.index}
            onClick={() => setActive(index)}
            type="button"
          >
            <span className="mr-1 opacity-60">{slide.index}</span>
            <span className="line-clamp-2">{slide.title}</span>
          </button>
        ))}
      </aside>
      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-[var(--divider)] px-3 py-2 text-xs">
          <button
            className="text-chat-muted hover:bg-primary/5 rounded px-2 py-0.5"
            disabled={active === 0}
            onClick={() => move(-1)}
            type="button"
          >
            {t("pptx.prev")}
          </button>
          <span className="text-chat-muted">
            {current ? `${current.index} / ${slides.length}` : `0 / ${slides.length}`}
          </span>
          <button
            className="text-chat-muted hover:bg-primary/5 rounded px-2 py-0.5"
            disabled={active >= slides.length - 1}
            onClick={() => move(1)}
            type="button"
          >
            {t("pptx.next")}
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-6">
          {current ? (
            <>
              <h2 className="text-chat-ink text-lg font-semibold">{current.title}</h2>
              <p className="text-chat-ink mt-3 whitespace-pre-wrap text-sm leading-7">
                {current.text}
              </p>
            </>
          ) : (
            <p className="text-chat-muted text-sm">{t("knowledge.loading")}</p>
          )}
        </div>
      </section>
    </div>
  );
}
