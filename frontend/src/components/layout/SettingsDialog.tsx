/**
  * 功能：设置弹窗（）。
 * -实装「模型档案」区（列表 + 激活 + 编辑 + 删除）——档案切换/删除按
 * 设置页统一承接配置（弹窗只做新增/编辑，避免双入口漂移）。
 * 弹窗改为**左侧导航 + 右侧内容**两栏——导航项带图标，可点选切换
  * （**用量与缓存** / 模型配置）。其余设置项（通用/权限档位/密钥与凭证/数据目录/数据保留/关于）仍留。
 * 注：用量与缓存**刻意合成一个页签**——同一份聚合数据/同一个台账，拆开只会重复展示命中率与重复请求。
 */
import { AlertDialog } from "radix-ui";
import {
  Boxes,
  Cable,
  ChartColumn,
  Check,
  Database,
  Gavel,
  Info,
  Monitor,
  Pencil,
  Plus,
  Settings,
  Shield,
  SlidersHorizontal,
  Trash2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { ModelConfigDialog } from "@/components/chat/ModelConfigDialog";
import {
  AboutPanel,
  CapabilitiesPanel,
  ComputerUsePanel,
  DataPanel,
  McpPanel,
  PermissionsPanel,
  SystemPanel,
} from "@/components/settings/SettingsPanels";
import { UsageCachePanel } from "@/components/settings/UsageCachePanel";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { ApiError } from "@/services/api";
import {
  EMPTY_USAGE_STATS,
  settingsApi,
  type LlmProfile,
  type ModelOption,
  type ProfileSummary,
  type ProfilesPayload,
  type UsageRange,
  type UsageStats,
} from "@/services/settings";
import { useUiStore } from "@/stores/ui-store";

interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** 弹窗左侧导航项。新增模块时在此追加即可——顺序即展示顺序。 */
type SettingsTab =
  | "system"
  | "usageCache"
  | "profiles"
  | "permissions"
  | "data"
  | "mcp"
  | "capabilities"
  | "computer"
  | "about";

const SETTINGS_NAV: readonly { id: SettingsTab; icon: LucideIcon; labelKey: string }[] = [
  { id: "system", icon: Settings, labelKey: "settings.nav.system" },
  { id: "usageCache", icon: ChartColumn, labelKey: "settings.nav.usageCache" },
  { id: "profiles", icon: SlidersHorizontal, labelKey: "settings.nav.profiles" },
  { id: "permissions", icon: Shield, labelKey: "settings.nav.permissions" },
  { id: "data", icon: Database, labelKey: "settings.nav.data" },
  { id: "mcp", icon: Cable, labelKey: "settings.nav.mcp" },
  { id: "capabilities", icon: Boxes, labelKey: "settings.nav.capabilities" },
  { id: "computer", icon: Monitor, labelKey: "settings.nav.computer" },
];

/** 功能：底部关于（不占导航主体，始终贴底部）。 */
const SETTINGS_ABOUT: { id: SettingsTab; icon: LucideIcon; labelKey: string } = {
  id: "about",
  icon: Info,
  labelKey: "settings.nav.about",
};

/** -修复：编辑态必须携带 LlmProfile 原始掩码 key 与 extra——
 * PUT 是全量替换，传空串会把密钥清空、漏字段会把 timeout/thinking 等配置清掉。 */
type EditableProfile = ProfileSummary & { extra?: Record<string, string | number> };

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** 功能：档案行内的删除确认（-；删除激活档案需显式确认，防误删唯一可用档案）。 */
function DeleteProfileButton({
  profile,
  onDeleted,
}: {
  profile: LlmProfile;
  onDeleted: (activeProfileId: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const remove = async () => {
    setBusy(true);
    try {
      const result = await settingsApi.deleteProfile(profile.profile_id);
      onDeleted(result.active_profile_id);
      // 删除档案会改变生效模型——同步广播给会话框控制条重取
      useUiStore.getState().bumpModelsVersion();
      setOpen(false);
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <AlertDialog.Root open={open} onOpenChange={setOpen}>
      <AlertDialog.Trigger asChild>
        <button
          type="button"
          data-testid={`profile-delete-${profile.profile_id}`}
          title={t("settings.profiles.delete")}
          aria-label={t("settings.profiles.delete")}
          className="text-chat-muted hover:text-destructive flex size-6 items-center justify-center rounded-md"
        >
          <Trash2 className="size-3.5" />
        </button>
      </AlertDialog.Trigger>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-50 bg-black/30" />
        <AlertDialog.Content className="bg-card border-chat-hairline fixed top-1/2 left-1/2 z-50 w-[min(420px,90vw)] -translate-x-1/2 -translate-y-1/2 rounded-xl border p-4 shadow-lg">
          <AlertDialog.Title className="text-chat-ink text-sm font-medium">
            {t("settings.profiles.deleteConfirmTitle")}
          </AlertDialog.Title>
          <AlertDialog.Description className="text-chat-muted mt-1 text-xs leading-relaxed">
            {t("settings.profiles.deleteConfirmBody").replace("{name}", profile.name)}
          </AlertDialog.Description>
          <div className="mt-3 flex justify-end gap-2">
            <AlertDialog.Cancel asChild>
              <Button variant="ghost" size="sm" className="h-7 px-3 text-xs">
                {t("common.cancel")}
              </Button>
            </AlertDialog.Cancel>
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-3 text-xs"
              data-testid={`profile-delete-confirm-${profile.profile_id}`}
              disabled={busy}
              onClick={() => void remove()}
            >
              {t("settings.profiles.delete")}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

/** 功能：模型档案区——列表 + 激活 + 编辑 + 删除 + 新增（编辑/新增复用配置弹窗）。 */
function ProfilesSection() {
  const t = useT();
  const [payload, setPayload] = useState<ProfilesPayload | null>(null);
  const [options, setOptions] = useState<ModelOption[]>([]);
  const [editing, setEditing] = useState<EditableProfile | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  // 反馈模型（反思验收 LLM-as-Judge）指定——点击档案行 icon → 确认弹窗
  const [feedbackTarget, setFeedbackTarget] = useState<
    (LlmProfile & { has_api_key?: boolean }) | null
  >(null);
  const [feedbackSaving, setFeedbackSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [profiles, model] = await Promise.all([settingsApi.profiles(), settingsApi.model()]);
      setPayload(profiles);
      setOptions(model.available_models ?? []);
    } catch (error) {
      toast.error(errorMessage(error));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const activate = async (profileId: string) => {
    try {
      const result = await settingsApi.activateProfile(profileId);
      setPayload((prev) =>
        prev ? { ...prev, active_profile_id: result.active_profile_id } : prev,
      );
      await load();
      // 通知会话框模型控制条重取——设置页切换档案后
      // 员工未配独立模型时会话应跟随新档案（此前只有 Composer 内弹窗会通知）
      useUiStore.getState().bumpModelsVersion();
    } catch (error) {
      toast.error(errorMessage(error));
    }
  };

  /** 功能：确认指定反馈模型（反思验收 LLM-as-Judge；成功后刷新选中态）。 */
  const confirmFeedback = async () => {
    if (!feedbackTarget) return;
    setFeedbackSaving(true);
    try {
      await settingsApi.setFeedbackProfile(feedbackTarget.profile_id);
      toast.success(
        t("settings.profiles.feedbackSaved").replace(
          "{name}",
          feedbackTarget.name || feedbackTarget.model,
        ),
      );
      setFeedbackTarget(null);
      await load();
    } catch (error) {
      toast.error(errorMessage(error));
    } finally {
      setFeedbackSaving(false);
    }
  };

  const profiles = payload?.profiles ?? [];
  const activeId = payload?.active_profile_id ?? "";
  const feedbackId = payload?.feedback_profile_id ?? "";

  return (
    <section data-testid="settings-profiles" className="mt-2">
      <div className="flex items-center justify-between">
        <h3 className="text-chat-ink text-xs font-medium">{t("settings.profiles.title")}</h3>
        <Button
          size="sm"
          data-testid="profile-create"
          className="h-7 gap-1 px-2 text-xs hover:bg-primary"
          onClick={() => {
            setEditing(null);
            setDialogOpen(true);
          }}
        >
          <Plus className="size-3.5" />
          {t("settings.profiles.create")}
        </Button>
      </div>
      {profiles.length === 0 ? (
        <p className="text-chat-muted mt-1.5 text-xs">{t("settings.profiles.empty")}</p>
      ) : (
        <ul className="mt-1.5 space-y-1">
          {profiles.map((profile) => {
            const active = profile.profile_id === activeId;
            const isFeedback = profile.profile_id === feedbackId;
            return (
              <li
                key={profile.profile_id}
                data-testid={`profile-row-${profile.profile_id}`}
                className="border-chat-hairline flex items-center gap-2 rounded-lg border px-2.5 py-1.5"
                /* （第二轮更正）：使用中的**模型条目胶囊**不再变灰——
                   去掉底色与底色描边（此前 primary/5 叠加读作「整行变灰、像被禁用」）。 */
              >
                {/*使用中 = **灰色 + 打勾**；未使用 = **黑色 + 无勾**
                    （此前反了：使用中彩色打勾、未使用浅灰，看起来像"不可用"）。
                    勾位纯状态展示（不可点），切换走右侧「应用」按钮。
使用中的**勾色换回主色**（用户「换回之前的样式」）。 */}
                <span
                  className="flex size-6 shrink-0 items-center justify-center rounded-md"
                  data-testid={`profile-active-mark-${profile.profile_id}`}
                  aria-hidden={!active}
                >
                  {active ? <Check className="text-primary size-3.5" /> : null}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-chat-ink truncate text-xs">
                    {profile.name}
                    {active ? (
                      <span className="text-chat-muted ml-1.5 text-[11px]">
                        {t("settings.profiles.active")}
                      </span>
                    ) : null}
                  </p>
                  <p className="text-chat-muted truncate text-[11px]">
                    {profile.provider} · {profile.model}
                    {profile.has_api_key === false ? ` · ${t("settings.profiles.noKey")}` : ""}
                  </p>
                </div>
                <button
                  type="button"
                  data-testid={`profile-edit-${profile.profile_id}`}
                  title={t("settings.profiles.edit")}
                  aria-label={t("settings.profiles.edit")}
                  className="text-chat-muted hover:text-chat-ink flex size-6 items-center justify-center rounded-md"
                  onClick={() => {
                    setEditing({
                      profile_id: profile.profile_id,
                      name: profile.name,
                      provider: profile.provider,
                      model: profile.model,
                      base_url: profile.base_url,
                      // 掩码值（含 ***）——后端据此保留仓库原 key；传空串会清空密钥
                      api_key_masked: profile.api_key,
                      extra: profile.extra,
                    });
                    setDialogOpen(true);
                  }}
                >
                  <Pencil className="size-3.5" />
                </button>
                {/* 反馈模型入口（反思验收 LLM-as-Judge）——编辑右、删除左；
                    已指定的档案高亮 + **禁用**（确定后不可重复选中，对齐「激活」交互） */}
                <button
                  type="button"
                  data-testid={`profile-feedback-${profile.profile_id}`}
                  disabled={isFeedback}
                  title={
                    isFeedback
                      ? t("settings.profiles.feedbackActive")
                      : t("settings.profiles.feedbackSet")
                  }
                  aria-label={t("settings.profiles.feedbackSet")}
                  className={cn(
                    "flex size-6 items-center justify-center rounded-md",
                    isFeedback ? "text-primary" : "text-chat-muted hover:text-chat-ink",
                  )}
                  onClick={() => setFeedbackTarget(profile)}
                >
                  <Gavel className="size-3.5" />
                </button>
                <DeleteProfileButton
                  profile={profile}
                  onDeleted={(nextActiveId) => {
                    setPayload((prev) =>
                      prev ? { ...prev, active_profile_id: nextActiveId } : prev,
                    );
                    void load();
                  }}
                />
                {/*改为**黑底白字**主按钮（与全局 `default` 变体同款）——
                    此前是黑边白底 outline，与其余主操作按钮不统一。
（第三轮）：「使用中」态改**卡片底色 + 同色边缘线**
                    （`bg-primary/5` + `border-primary/5`）——disabled 黑按钮的灰饼感仍太重。 */}
                <Button
                  variant="default"
                  size="sm"
                  disabled={active}
                  data-testid={`profile-activate-${profile.profile_id}`}
                  title={active ? t("settings.profiles.active") : t("settings.profiles.apply")}
                  className={cn(
                    "h-6 shrink-0 px-2 text-[11px]",
                    active && "border-primary/5 bg-primary/5 text-chat-muted disabled:opacity-100",
                  )}
                  onClick={() => void activate(profile.profile_id)}
                >
                  {active ? t("settings.profiles.active") : t("settings.profiles.apply")}
                </Button>
              </li>
            );
          })}
        </ul>
      )}
      {/* 反馈模型确认弹窗（标题 + 说明 + 右下角取消/确认） */}
      <Dialog
        open={feedbackTarget !== null}
        onOpenChange={(next) => {
          if (!next) setFeedbackTarget(null);
        }}
      >
        <DialogContent className="sm:max-w-md" data-testid="feedback-model-dialog">
          <DialogHeader>
            <DialogTitle>{t("settings.profiles.feedbackTitle")}</DialogTitle>
            <DialogDescription className="text-xs leading-relaxed">
              {t("settings.profiles.feedbackHint")}
            </DialogDescription>
          </DialogHeader>
          {feedbackTarget ? (
            <div className="border-chat-hairline bg-chat-rail/40 flex items-center gap-2 rounded-lg border px-3 py-2">
              <Gavel className="text-chat-muted size-3.5 shrink-0" />
              <span className="text-chat-ink min-w-0 flex-1 truncate text-xs">
                {feedbackTarget.name || feedbackTarget.model}
              </span>
              <span className="text-chat-muted shrink-0 text-[11px]">
                {feedbackTarget.provider} · {feedbackTarget.model}
              </span>
            </div>
          ) : null}
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-3 text-xs"
              data-testid="feedback-model-cancel"
              onClick={() => setFeedbackTarget(null)}
            >
              {t("common.cancel")}
            </Button>
            <Button
              size="sm"
              className="h-7 px-3 text-xs"
              data-testid="feedback-model-confirm"
              disabled={feedbackSaving}
              onClick={() => void confirmFeedback()}
            >
              {t("common.confirm")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <ModelConfigDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        profile={editing}
        options={options}
        onChanged={() => void load()}
      />
    </section>
  );
}

/** 功能：导航项（图标 + 文案；选中态与 hover 沿用弹窗既有语言）。 */
function NavItem({
  item,
  active,
  onPick,
}: {
  item: { id: SettingsTab; icon: LucideIcon; labelKey: string };
  active: boolean;
  onPick: (id: SettingsTab) => void;
}) {
  const t = useT();
  const Icon = item.icon;
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      data-testid={`settings-nav-${item.id}`}
      onClick={() => onPick(item.id)}
      className={cn(
        "flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs",
        active ? "bg-primary/5 text-chat-ink font-medium" : "text-chat-muted hover:text-chat-ink",
      )}
    >
      <Icon className="size-3.5 shrink-0" />
      {t(item.labelKey)}
    </button>
  );
}

/** 功能：设置弹窗（两栏：左侧导航 + 右侧内容；模型档案区沿用 -实现）。 */
export function SettingsDialog({ open, onOpenChange }: SettingsDialogProps) {
  const t = useT();
  const [tab, setTab] = useState<SettingsTab>("system");
  const [range, setRange] = useState<UsageRange>("all");
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [loading, setLoading] = useState(false);

  // 关闭即丢弃聚合数据：下次打开必然重取。否则「关掉设置 → 用一会儿 → 再打开」会看到
  // 陈旧数字（比多一次本地 SQLite 查询更糟）。
  useEffect(() => {
    if (!open) setStats(null);
  }, [open]);

  // 用量与缓存两页签**共用同一次请求**：只在「当前页签需要数据」且「尚无该区间数据」时取。
  // 切页签不重复请求；切区间才重取（stats.range 与所选区间不一致即视为需重取）。
  useEffect(() => {
    if (!open) return;
    if (tab !== "usageCache") return;
    if (stats && stats.range === range) return;
    let cancelled = false;
    setLoading(true);
    settingsApi
      .usageStats(range)
      .then((data) => {
        if (!cancelled) setStats(data);
      })
      .catch((error: unknown) => {
        if (!cancelled) toast.error(errorMessage(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, tab, range, stats]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[min(640px,85vh)] flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="border-chat-hairline shrink-0 border-b px-4 py-3 pr-12">
          <DialogTitle>{t("nav.settings")}</DialogTitle>
          <DialogDescription className="sr-only">{t("settings.placeholder")}</DialogDescription>
        </DialogHeader>
        <div className="flex min-h-0 flex-1">
          <nav
            role="tablist"
            aria-label={t("nav.settings")}
            data-testid="settings-nav"
            className="border-chat-hairline flex w-36 shrink-0 flex-col gap-0.5 overflow-y-auto border-r p-2"
          >
            {SETTINGS_NAV.map((item) => (
              <NavItem key={item.id} item={item} active={tab === item.id} onPick={setTab} />
            ))}
            <span className="flex-1" />
            <span className="border-chat-hairline my-1 border-t" />
            <NavItem item={SETTINGS_ABOUT} active={tab === SETTINGS_ABOUT.id} onPick={setTab} />
          </nav>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            {tab === "system" ? <SystemPanel /> : null}
            {tab === "usageCache" ? (
              <UsageCachePanel
                stats={stats ?? EMPTY_USAGE_STATS}
                range={range}
                onRangeChange={setRange}
                loading={loading}
              />
            ) : null}
            {tab === "profiles" ? open ? <ProfilesSection /> : null : null}
            {tab === "permissions" ? <PermissionsPanel /> : null}
            {tab === "data" ? <DataPanel /> : null}
            {tab === "mcp" ? <McpPanel /> : null}
            {tab === "capabilities" ? <CapabilitiesPanel /> : null}
            {tab === "computer" ? <ComputerUsePanel /> : null}
            {tab === "about" ? <AboutPanel /> : null}
          </div>
        </div>
        <p className="text-chat-muted border-chat-hairline flex shrink-0 items-center gap-2 border-t px-4 py-2.5 text-[11px]">
          <Settings className="size-3.5 shrink-0 opacity-40" />
          {t("settings.placeholderBody")}
        </p>
      </DialogContent>
    </Dialog>
  );
}
