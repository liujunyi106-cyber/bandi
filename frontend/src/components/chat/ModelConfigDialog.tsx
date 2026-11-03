/**
  * 功能：模型配置弹窗——provider/模型/API Key/Base URL
 * + 高级项折叠区（灰字段，默认收起）。编辑态额外提供 设为当前 / 删除。
 * 掩码约定：编辑态回显的是 API 掩码值；未改动直接保存时后端按掩码识别并保留原 key。
 */
import { ChevronDown, Eye, EyeOff, Loader2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { OFFICIAL_PROVIDERS, providerLabel } from "@/lib/provider-labels";
import { ProviderLogo } from "@/components/chat/ProviderLogo";
import { useUiStore } from "@/stores/ui-store";
import {
  settingsApi,
  type LlmProfile,
  type ModelOption,
  type ProfileSummary,
} from "@/services/settings";

interface ModelConfigDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 编辑态 = 传入激活档案；null = 新建（创建后自动激活）。 */
  profile: (ProfileSummary & { extra?: Record<string, string | number> }) | null;
  options: ModelOption[];
  /** 保存/删除/激活成功后回调（父层刷新数据源）。 */
  onChanged: (profileId: string) => void;
}

interface ProfileFormState {
  name: string;
  provider: string;
  model: string;
  api_key: string;
  base_url: string;
  timeout: string;
  max_output_tokens: string;
  thinking_mode: string;
}

const EMPTY_FORM: ProfileFormState = {
  name: "",
  provider: "",
  model: "",
  api_key: "",
  base_url: "",
  timeout: "",
  max_output_tokens: "",
  thinking_mode: "",
};

export function ModelConfigDialog({
  open,
  onOpenChange,
  profile,
  options,
  onChanged,
}: ModelConfigDialogProps) {
  const t = useT();
  const [form, setForm] = useState<ProfileFormState>(EMPTY_FORM);
  const [showKey, setShowKey] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  // 自定义 provider 输入（下拉之外允许自定义网关/新厂商）
  const [customProvider, setCustomProvider] = useState(false);
  const CUSTOM_PROVIDER = "__custom__";

  // 打开时按编辑态回填（编辑态回显掩码 key——后端按掩码保留原值）
  useEffect(() => {
    if (!open) return;
    setError("");
    setShowKey(false);
    setAdvancedOpen(false);
    if (profile) {
      // -修复：extra 三字段从档案回填（PUT 全量替换——漏填会清掉 timeout/thinking 配置）
      const extra = profile.extra ?? {};
      setForm({
        name: profile.name,
        provider: profile.provider,
        model: profile.model,
        api_key: profile.api_key_masked,
        base_url: profile.base_url,
        timeout: extra.timeout != null ? String(extra.timeout) : "",
        max_output_tokens: extra.max_output_tokens != null ? String(extra.max_output_tokens) : "",
        thinking_mode: extra.thinking_mode != null ? String(extra.thinking_mode) : "",
      });
      // 编辑态：provider 不在官方清单 ⇒ 进入自定义输入态
      setCustomProvider(!OFFICIAL_PROVIDERS.includes(profile.provider));
    } else {
      setForm(EMPTY_FORM);
      setCustomProvider(false);
    }
  }, [open, profile]);

  const modelsForProvider = useMemo(
    // 模型候选全段字母排序（按名称 localeCompare）
    () =>
      options
        .filter((option) => !form.provider || option.provider === form.provider)
        .sort((a, b) => a.name.localeCompare(b.name)),
    [options, form.provider],
  );

  const set = (patch: Partial<ProfileFormState>) => setForm((state) => ({ ...state, ...patch }));

  const save = async () => {
    if (!form.model.trim()) {
      setError(t("chat.composer.controls.profileErrorModel"));
      return;
    }
    setSaving(true);
    setError("");
    try {
      const extra: Record<string, string | number> = {};
      if (form.thinking_mode.trim()) extra.thinking_mode = form.thinking_mode.trim();
      if (form.timeout.trim()) extra.timeout = Number(form.timeout);
      if (form.max_output_tokens.trim()) extra.max_output_tokens = Number(form.max_output_tokens);

      const payload = {
        name: form.name.trim(),
        provider: form.provider.trim(),
        model: form.model.trim(),
        api_key: form.api_key,
        base_url: form.base_url.trim(),
        extra,
      };
      if (profile) {
        await settingsApi.updateProfile(profile.profile_id, payload);
      } else {
        const profileId = `p-${Date.now().toString(36)}`;
        await settingsApi.createProfile({ ...payload, profile_id: profileId });
        await settingsApi.activateProfile(profileId);
        onChanged(profileId);
      }
      if (profile) onChanged(profile.profile_id);
      // 保存成功即广播：会话框模型控制条据此重取——设置页与对话框两条配置路径
      // 都能即时生效，不依赖调用方透传回调（桌面端无刷新入口，实时是唯一口径）
      useUiStore.getState().bumpModelsVersion();
      toast.success(t("chat.composer.controls.profileSaved"));
      onOpenChange(false);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex max-h-[85vh] flex-col gap-3 sm:max-w-md"
        data-testid="model-config-dialog"
      >
        <DialogHeader>
          <DialogTitle>
            {profile
              ? t("chat.composer.controls.profileEdit")
              : t("chat.composer.controls.profileTitle")}
          </DialogTitle>
        </DialogHeader>

        {/* 字段区 = 唯一滚动容器（2026-09-24 三轮反馈：滚动条不得贴弹窗圆角边框 ⇒
            滚动收进内层 + pr 留距；页头/页脚固定不参与滚动） */}
        <div className="flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto pr-1.5">
          <label className="flex flex-col gap-1">
            <span className="text-chat-muted text-xs">
              {t("chat.composer.controls.profileName")}
            </span>
            <Input
              value={form.name}
              onChange={(event) => set({ name: event.target.value })}
              placeholder={t("chat.composer.controls.profileNamePlaceholder")}
              data-testid="profile-name"
            />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-chat-muted text-xs">
              {t("chat.composer.controls.profileProvider")}
            </span>
            {/* 用户反馈：供应商下拉长度须与下方输入框一致
                （Input 为 w-full，SelectTrigger 默认 w-fit） */}
            <Select
              value={customProvider ? CUSTOM_PROVIDER : form.provider}
              onValueChange={(value) => {
                if (value === CUSTOM_PROVIDER) {
                  setCustomProvider(true);
                  set({ provider: "", model: "" });
                } else {
                  setCustomProvider(false);
                  set({ provider: value, model: "" });
                }
              }}
            >
              <SelectTrigger
                data-testid="profile-provider"
                className="[&_svg]:size-3.5! [&_svg]:text-muted-foreground! w-full"
              >
                <SelectValue placeholder={t("chat.composer.controls.profileProviderPlaceholder")} />
              </SelectTrigger>
              <SelectContent>
                {OFFICIAL_PROVIDERS.map((provider) => (
                  <SelectItem key={provider} value={provider}>
                    <ProviderLogo provider={provider} className="size-3.5" />
                    {providerLabel(provider)}
                  </SelectItem>
                ))}
                <SelectItem value={CUSTOM_PROVIDER}>
                  {t("chat.composer.controls.providerCustom")}
                </SelectItem>
              </SelectContent>
            </Select>
            {customProvider ? (
              <Input
                value={form.provider}
                onChange={(event) => set({ provider: event.target.value })}
                placeholder={t("chat.composer.controls.providerCustomPlaceholder")}
                data-testid="profile-provider-custom"
                className="mt-1"
              />
            ) : null}
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-chat-muted text-xs">
              {t("chat.composer.controls.profileModel")}
            </span>
            <Input
              value={form.model}
              list="profile-model-options"
              onChange={(event) => set({ model: event.target.value })}
              placeholder={t("chat.composer.controls.profileModelPlaceholder")}
              data-testid="profile-model"
            />
            <datalist id="profile-model-options">
              {modelsForProvider.map((option) => (
                <option key={option.name} value={option.name} />
              ))}
            </datalist>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-chat-muted text-xs">
              {t("chat.composer.controls.profileApiKey")}
            </span>
            <div className="relative">
              <Input
                type={showKey ? "text" : "password"}
                value={form.api_key}
                onChange={(event) => set({ api_key: event.target.value })}
                placeholder="sk-..."
                className="pr-9"
                data-testid="profile-api-key"
              />
              <button
                type="button"
                aria-label={t("chat.composer.controls.profileToggleKey")}
                className="text-chat-muted hover:text-chat-ink absolute inset-y-0 right-2 my-auto"
                onClick={() => setShowKey((state) => !state)}
              >
                {showKey ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
              </button>
            </div>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-chat-muted text-xs">
              {t("chat.composer.controls.profileBaseUrl")}
            </span>
            <Input
              value={form.base_url}
              onChange={(event) => set({ base_url: event.target.value })}
              placeholder="https://..."
              data-testid="profile-base-url"
            />
          </label>

          {/* 高级项折叠区（DP-137b2b-3：灰字段，默认收起） */}
          <button
            type="button"
            className="text-chat-muted hover:text-chat-ink self-start text-xs"
            data-testid="profile-advanced-toggle"
            onClick={() => setAdvancedOpen((state) => !state)}
          >
            <ChevronDown
              className={cn("inline size-3.5 transition-transform", advancedOpen && "rotate-180")}
            />{" "}
            {t("chat.composer.controls.profileAdvanced")}
          </button>
          {advancedOpen ? (
            <div className="flex flex-col gap-2.5 rounded-lg border border-dashed p-2.5 opacity-80">
              <label className="flex flex-col gap-1">
                <span className="text-chat-muted text-xs">
                  {t("chat.composer.controls.profileThinkingMode")}
                </span>
                <Input
                  value={form.thinking_mode}
                  onChange={(event) => set({ thinking_mode: event.target.value })}
                  placeholder="low / medium / high / max"
                  data-testid="profile-thinking-mode"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-chat-muted text-xs">
                  {t("chat.composer.controls.profileTimeout")}
                </span>
                <Input
                  type="number"
                  min={1}
                  value={form.timeout}
                  onChange={(event) => set({ timeout: event.target.value })}
                  data-testid="profile-timeout"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-chat-muted text-xs">
                  {t("chat.composer.controls.profileMaxOutput")}
                </span>
                <Input
                  type="number"
                  min={1}
                  value={form.max_output_tokens}
                  onChange={(event) => set({ max_output_tokens: event.target.value })}
                  data-testid="profile-max-output"
                />
              </label>
            </div>
          ) : null}

          {error ? (
            <div className="text-destructive text-xs" data-testid="profile-error">
              {error}
            </div>
          ) : null}
        </div>

        {/* 用户反馈：页脚与上方分区过重 ⇒ 去灰底、分隔线改最浅 hairline */}
        <DialogFooter className="border-chat-hairline/60 bg-transparent mt-1 flex items-center gap-2">
          {/*本弹窗只负责「新增/编辑模型配置」——
              设为当前（切换激活）与删除移到「设置」里做，弹窗内不再提供 */}
          <span className="flex-1" />
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            data-testid="profile-save"
            disabled={saving}
            onClick={() => void save()}
          >
            {saving ? <Loader2 className="size-4 animate-spin" /> : null}
            {t("chat.composer.controls.profileSave")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** 类型再导出（父层只关心档案形状，不关心弹窗内部实现）。 */
export type { LlmProfile, ProfileSummary };
