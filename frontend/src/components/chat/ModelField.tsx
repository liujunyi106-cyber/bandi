/**
 * 功能：模型字段：
 * - **切换 = 全局切换**：点候选档案即激活该档案（改全局默认，所有会话/员工跟随）
 * 不再有「会话级临时覆盖」，「跟随档案/全局默认」行随之移除（单一语义）。
 * - **员工独立模型锁定**：员工配了 llm_config.default_model 时（单聊/员工视角）
 * 字段显示该员工实际使用的模型，切换入口禁用并说明——全局切换对它无效
 * 避免"改了却没生效"的误导。
 * - 列表数据源 = 目录模型 ∪ 已配置档案（lib/model-options），网关私有模型可见。
 */
import { ChevronDown, Lock, Settings2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { useT } from "@/i18n";
import { normalizeModelKey } from "@/lib/model-key";
import { cn } from "@/lib/utils";
import { ProviderLogo } from "@/components/chat/ProviderLogo";
import type { ModelOption, ProfileSummary } from "@/services/settings";

/** 功能：按模型键查目录（归一化匹配——网关前缀差异）。 */
function findOption(options: ModelOption[], name: string): ModelOption | undefined {
  return options.find(
    (option) => option.name === name || normalizeModelKey(option.name) === normalizeModelKey(name),
  );
}

interface ModelFieldProps {
  /** 当前生效模型（档案激活时父层传档案模型）。 */
  model: string;
  options: ModelOption[];
  activeProfile: ProfileSummary | null;
  /** 已配置档案（含 profile_id/name/provider）——A 方案：点候选 = 激活该档案。 */
  profiles?: ProfileSummary[];
  /**
   * 功能：员工独立模型（非空 = 该员工用专属模型，全局切换无效 → 锁定显示）。
   * 由单聊/员工视角传入（employee.llm_config.default_model）。
   */
  employeeModel?: string;
  /** 功能：切换档案（全局生效）。缺省 = 只读展示。 */
  onActivate?: (profileId: string) => void;
  /**
   * 功能：打开配置弹窗——`create` = 新建档案（底部「配置模型」入口）；
   * `edit` = 编辑当前生效档案（点击当前生效行）。
   */
  onOpenConfig: (mode: "create" | "edit") => void;
  /** 递增信号：外部（model 斜杠命令）请求展开模型下拉。 */
  openSignal?: number;
}

export function ModelField({
  model,
  options,
  activeProfile,
  profiles = [],
  employeeModel = "",
  onActivate,
  onOpenConfig,
  openSignal = 0,
}: ModelFieldProps) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (openSignal > 0) setOpen(true);
  }, [openSignal]);

  useEffect(() => {
    if (!open) return;
    const onMouseDown = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onMouseDown);
    return () => document.removeEventListener("mousedown", onMouseDown);
  }, [open]);

  /** 员工锁定：配了独立模型 → 显示员工模型 + 禁止全局切换。 */
  const employeeLocked = Boolean(employeeModel);

  const provider = useMemo(() => {
    if (employeeLocked) return "";
    if (activeProfile) return activeProfile.provider;
    return findOption(options, model)?.provider ?? "";
  }, [activeProfile, options, model, employeeLocked]);

  const configured = Boolean(activeProfile || model);
  const label = employeeLocked
    ? employeeModel
    : activeProfile
      ? activeProfile.name || activeProfile.model
      : model || t("chat.composer.controls.modelNotConfigured");

  // 切换候选 = 已配置档案（点即激活；A 方案只提供档案级全局切换）
  const switchableProfiles = useMemo(
    () =>
      profiles.filter(
        (profile) =>
          (profile as { has_api_key?: boolean }).has_api_key !== false &&
          normalizeModelKey(profile.model) !== normalizeModelKey(model),
      ),
    [profiles, model],
  );

  return (
    <div className="relative min-w-0" ref={wrapRef}>
      <button
        type="button"
        data-testid="composer-model"
        title={employeeLocked ? t("chat.composer.controls.employeeModelLocked") : label}
        className="no-focus-ring border-primary/5 text-chat-muted hover:text-chat-ink flex max-w-44 min-w-0 items-center gap-1 rounded-full border-[1.5px] px-2.5 py-1 text-xs"
        onClick={() => setOpen((state) => !state)}
      >
        {employeeLocked ? (
          <Lock className="size-3 shrink-0" />
        ) : configured && provider ? (
          <ProviderLogo provider={provider} className="size-3.5" />
        ) : null}
        <span className="truncate">{label}</span>
        <ChevronDown
          className={cn("size-3.5 shrink-0 transition-transform", open && "rotate-180")}
          data-testid="composer-model-arrow"
        />
      </button>

      {open ? (
        <div
          data-testid="composer-model-menu"
          className="border-chat-hairline bg-card absolute right-0 bottom-full z-50 mb-2 flex w-64 flex-col rounded-xl border p-1 shadow-md"
        >
          {/* 当前生效行（点 = 编辑当前档案；员工锁定时仅展示） */}
          {configured ? (
            <button
              type="button"
              data-testid="model-option-current"
              className="bg-primary/5 text-chat-ink flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm"
              onClick={() => {
                if (activeProfile && !employeeLocked) {
                  setOpen(false);
                  onOpenConfig("edit");
                } else {
                  setOpen(false);
                }
              }}
            >
              {employeeLocked ? (
                <Lock className="size-3.5 shrink-0" />
              ) : provider ? (
                <ProviderLogo provider={provider} className="size-3.5" />
              ) : null}
              <span className="truncate">{employeeLocked ? employeeModel : model}</span>
              <span className="text-chat-muted ml-auto shrink-0 text-[11px]">
                {employeeLocked
                  ? t("chat.composer.controls.employeeOwnModel")
                  : activeProfile
                    ? t("chat.composer.controls.profileActive")
                    : t("chat.composer.controls.currentModel")}
              </span>
            </button>
          ) : (
            <div className="text-chat-muted px-2.5 pt-2.5 pb-1 text-xs">
              {t("chat.composer.controls.modelNotConfiguredHint")}
            </div>
          )}

          {/*模型列表与「当前模型」同属模型区，不画分割线；
              仅底部「配置模型」入口用上边框区分（功能区 vs 模型区）。 */}
          {employeeLocked ? (
            <p className="text-chat-muted px-2.5 py-2 text-[11px] leading-relaxed">
              {t("chat.composer.controls.employeeModelHint")}
            </p>
          ) : switchableProfiles.length ? (
            <div data-testid="model-option-list" className="mt-1 max-h-56 overflow-y-auto pt-1">
              {switchableProfiles.map((profile) => (
                <button
                  key={profile.profile_id}
                  type="button"
                  data-testid={`model-option-${profile.model}`}
                  className="text-chat-ink hover:bg-primary/5 flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm"
                  onClick={() => {
                    onActivate?.(profile.profile_id);
                    setOpen(false);
                  }}
                >
                  <ProviderLogo provider={profile.provider} className="size-3.5" />
                  <span className="truncate">{profile.name || profile.model}</span>
                </button>
              ))}
            </div>
          ) : null}

          {/* 配置入口：常驻底部（唯一带分割线的行） */}
          <button
            type="button"
            data-testid="composer-model-config"
            className="border-chat-hairline text-chat-muted hover:text-chat-ink mt-1 flex w-full shrink-0 items-center gap-2 rounded-lg border-t px-2.5 py-2 text-left text-sm"
            onClick={() => {
              setOpen(false);
              onOpenConfig("create");
            }}
          >
            <Settings2 className="size-3.5 shrink-0" />
            <span className="truncate">{t("chat.composer.controls.modelConfig")}</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}
