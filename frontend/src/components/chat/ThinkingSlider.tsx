/**
 * 功能：思考挡位滑杆字段（Day137b-2b cc-haha 同款滑杆形态）。
 * 字段态 = 挡位名 + 箭头（不显示「思考：」前缀）；弹出 =
 * 当前档位名 + 注释行（随挡位实时变化）+ 椭圆胶囊轨道（内部白钮 thumb + 挡位点
 * 覆盖在胶囊内——DOM 层与 range 同行，点按 thumb 位置公式对齐）。档位 = ["", ...efforts]。
 * 键盘 ←→/Home/End 由原生 range 承担，Esc 关闭；reduced-motion 由 index.css 兜底。
 */
import { ChevronDown } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useT } from "@/i18n";
import { cn } from "@/lib/utils";

export interface ThinkingDegree {
  /** "" = 关闭（provider 侧映射为 disabled） */
  value: string;
  label: string;
  hint: string;
}

/** effort → 注释文案的 i18n 键（未知 effort 注释留空，字段名仍显示原名——） */
const EFFORT_HINT_KEYS: Record<string, string> = {
  low: "chat.composer.controls.effortHintLow",
  medium: "chat.composer.controls.effortHintMedium",
  high: "chat.composer.controls.effortHintHigh",
  max: "chat.composer.controls.effortHintMax",
};

interface ThinkingSliderProps {
  /** 当前 thinking_mode（"" = 关闭）。 */
  value: string;
  /** 当前模型支持的挡位（models.dev 同步 / 离线兜底，来自模型目录）。 */
  efforts: string[];
  /** -C：模型能否关闭思考（false = 强制思考，不渲染「关闭」档；默认 true）。 */
  offSupported?: boolean;
  disabled?: boolean;
  onChange: (mode: string) => void;
}

export function ThinkingSlider({
  value,
  efforts,
  offSupported = true,
  disabled = false,
  onChange,
}: ThinkingSliderProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const fieldRef = useRef<HTMLButtonElement>(null);

  const degrees = useMemo<ThinkingDegree[]>(
    () => [
      // -C：强制思考的模型不渲染「关闭」档（选了也关不掉——假关闭比没有更糟）
      ...(offSupported
        ? [
            {
              value: "",
              label: t("chat.composer.controls.thinkingOff"),
              hint: t("chat.composer.controls.effortHintOff"),
            },
          ]
        : []),
      // models.dev 的 "none" = 关闭档语义（与上方关闭档重复）——统一过滤
      // 所有模型（含目录同步后的 7 档）都不再出现多余 "none" 档
      ...efforts
        .filter((effort) => effort && effort !== "none")
        .map((effort) => ({
          value: effort,
          label: effort,
          hint: EFFORT_HINT_KEYS[effort] ? t(EFFORT_HINT_KEYS[effort]) : "",
        })),
    ],
    [efforts, offSupported, t],
  );

  const index = Math.max(
    0,
    degrees.findIndex((degree) => degree.value === value),
  );
  const current = degrees[index] ?? degrees[0];
  // 填充层宽度基准（index 0=关闭 时不填充，由调用处内联 0%）
  const fillPercent = degrees.length > 1 ? (index / (degrees.length - 1)) * 100 : 0;

  // 外部点击关闭（与 ⊕ 菜单同款交互）
  useEffect(() => {
    if (!open) return;
    const onMouseDown = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [open]);

  const close = () => {
    setOpen(false);
    fieldRef.current?.focus();
  };

  return (
    <div className="relative" ref={wrapRef}>
      <button
        ref={fieldRef}
        type="button"
        data-testid="composer-thinking"
        data-mode={value || "off"}
        disabled={disabled}
        title={
          disabled
            ? t("chat.composer.controls.thinkingUnsupported")
            : t("chat.composer.controls.thinking")
        }
        className={cn(
          "no-focus-ring border-primary/5 text-chat-muted hover:text-chat-ink flex max-w-36 min-w-0 items-center gap-1 rounded-full border-[1.5px] px-2.5 py-1 text-xs",
          disabled && "cursor-not-allowed opacity-40",
        )}
        onClick={() => setOpen((state) => !state)}
      >
        <span className="truncate">{current?.label}</span>
        <ChevronDown
          className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-180")}
        />
      </button>

      {open && !disabled ? (
        <div
          data-testid="composer-thinking-menu"
          role="dialog"
          aria-label={t("chat.composer.controls.thinking")}
          className="border-chat-hairline bg-card absolute right-0 bottom-full z-50 mb-2 w-72 rounded-xl border p-1 shadow-md"
          onKeyDown={(event) => {
            if (event.key === "Escape") close();
          }}
        >
          {/* 2026-09-26 用户反馈：弹层信息不如「上传文件列表」（ComposerMentions 条目 text-sm）
              大气 → 字号向它对齐：档位名 text-xs→text-sm、说明 11px→text-xs，行高同步放大 */}
          <div className="px-2.5 pt-2.5">
            <div className="text-chat-ink text-sm font-medium" data-testid="thinking-degree-name">
              {current?.label}
            </div>
            <div
              className="text-chat-muted min-h-5 text-xs leading-5"
              data-testid="thinking-degree-hint"
            >
              {current?.hint}
            </div>
          </div>
          <div className="px-2.5 pt-2 pb-1">
            {/* 胶囊（shell 背景 + 已滑过部分填充变色）+ 透明 range（白钮 thumb）+
                灰色竖条刻度层（首尾不放，与胶囊垂直居中；2026-09-24 二轮真机反馈）。
                刻度 left 按 range thumb 中心公式对齐（thumb 14px）。
                填充宽度：关闭档 = 0；**最大档 = 100%（必须填满——否则白钮右侧留 7px 缺口，
                2026-09-24 三轮反馈）**；中间档 = thumb 中心对齐。 */}
            <div className="thinking-shell">
              <div
                aria-hidden
                className="thinking-fill"
                style={{
                  width:
                    index === 0
                      ? "0%"
                      : index === degrees.length - 1
                        ? "100%"
                        : `calc(${fillPercent}% + ${(7 * (1 - fillPercent / 50)).toFixed(2)}px)`,
                }}
              />
              <input
                type="range"
                min={0}
                max={Math.max(0, degrees.length - 1)}
                step={1}
                value={index}
                aria-label={t("chat.composer.controls.thinking")}
                aria-valuetext={current?.label}
                data-testid="thinking-slider"
                className="thinking-slider"
                onChange={(event) => {
                  const degree = degrees[Number(event.target.value)];
                  if (degree) onChange(degree.value);
                }}
              />
              {/* 刻度层：2026-09-29 三轮反馈重写——① 颜色统一（不再按已滑过/未滑过分色，
                  深浅差被读作粗细差）；② 定位从 JS 百分比绝对定位改为 flex 均分：
                  容器 px-[6px] + justify-between 使刻度中心 = 7 + i*(W-14)/(n-1)，
                  与 range thumb 中心（半径 7px）精确对齐，且落点无亚像素小数
                  （旧式 calc(% + nudge px) 每条小数位不同 → 抗锯齿把 2px 线糊成 2~3px）。
                  首尾与当前档渲染透明占位以保持等距（用户拍板首尾不放实心竖条）。 */}
              <div
                aria-hidden
                className="pointer-events-none absolute inset-0 z-20 flex items-center justify-between px-[6px]"
              >
                {degrees.map((degree, degreeIndex) => {
                  const visible =
                    degreeIndex !== 0 &&
                    degreeIndex !== degrees.length - 1 &&
                    degreeIndex !== index;
                  return (
                    <span
                      key={degree.value || "off"}
                      data-testid={`thinking-option-${degree.value || "off"}`}
                      className={cn(
                        "h-2 w-0.5 shrink-0 rounded-full",
                        visible ? "bg-chat-ink/25" : "bg-transparent",
                      )}
                    />
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
