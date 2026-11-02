/**
 * 功能：分段选择器（黑色胶囊指示器，切换时平滑移动；用作筛选项与页签）。
 */
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

export interface SegmentedTabItem {
  value: string;
  label: string;
}

export interface SegmentedTabsProps {
  items: SegmentedTabItem[];
  value: string;
  onChange: (value: string) => void;
  ariaLabel?: string;
  className?: string;
}

interface Indicator {
  left: number;
  width: number;
}

/** 功能：胶囊滑动选择器。 */
export function SegmentedTabs({
  items,
  value,
  onChange,
  ariaLabel,
  className,
}: SegmentedTabsProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonsRef = useRef(new Map<string, HTMLButtonElement>());
  const [indicator, setIndicator] = useState<Indicator>({ left: 0, width: 0 });

  useLayoutEffect(() => {
    const target = buttonsRef.current.get(value);
    if (!target) return;
    setIndicator({ left: target.offsetLeft, width: target.offsetWidth });
  }, [items, value]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const target = buttonsRef.current.get(value);
      if (target) setIndicator({ left: target.offsetLeft, width: target.offsetWidth });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [value]);

  return (
    <div
      ref={containerRef}
      role="tablist"
      aria-label={ariaLabel}
      className={cn("bg-primary/5 relative inline-flex items-center rounded-full p-1", className)}
    >
      <span
        aria-hidden="true"
        className="bg-primary absolute top-1 bottom-1 left-0 rounded-full transition-[transform,width] duration-250 ease-out"
        style={{ transform: `translateX(${indicator.left}px)`, width: indicator.width }}
      />
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            ref={(element) => {
              if (element) buttonsRef.current.set(item.value, element);
              else buttonsRef.current.delete(item.value);
            }}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(item.value)}
            className={cn(
              "relative z-10 rounded-full px-3 py-1 text-xs transition-colors",
              active ? "text-primary-foreground" : "text-chat-muted hover:text-chat-ink",
            )}
          >
            {item.label}
          </button>
        );
      })}
    </div>
  );
}
