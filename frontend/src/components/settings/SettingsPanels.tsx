/**
  * 功能：设置弹窗各页签内容——系统 / 权限 / 数据 / MCP / 能力 / Computer Use / 关于。
 * 数据全部走真实端点（/settings/preferences、/system/*、/mcp/*），无本地伪造值；
 * 未实现能力（Computer Use 工具面）显式标注「开发中」，不做点了没反应的按钮。
 */
import { ArrowUpRight, Cable, ChevronDown, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import {
  PRESET_ORDER,
  currentPresetKey,
  isEmptyDiff,
  presetDiff,
  snapshotOf,
  type PermissionSnapshot,
  type PresetDiff,
} from "@/lib/permission-presets";
import { ApiError } from "@/services/api";
import {
  settingsApi,
  type ApprovalGrant,
  type ComputerStatus,
  type McpServer,
  type McpServersPayload,
  type PermissionPreset,
  type PermissionRule,
  type PermissionRuleAction,
  type PreferenceState,
  type SkillMeta,
  type StorageInfo,
  type SystemInfo,
  type ToolMeta,
} from "@/services/settings";
import { useUiStore } from "@/stores/ui-store";

const FONT_STEPS = [
  { key: "small", scale: 0.9 },
  { key: "smaller", scale: 0.95 },
  { key: "standard", scale: 1 },
  { key: "larger", scale: 1.1 },
  { key: "large", scale: 1.2 },
  { key: "huge", scale: 1.3 },
] as const;

/** 功能：页签标识——档位 / 放行规则（规则页下半部含临时放行）。 */
type PermissionTab = "presets" | "rules";

/** 功能：状态条档位徽标配色（含「自定义」态）。 */
const PRESET_CHIP: Record<string, string> = {
  loose: "rounded border border-[#d5e9dd] bg-[#eef6f1] px-1.5 py-0.5 text-[10.5px] font-semibold text-[#1c6b45]",
  standard:
    "rounded border border-[#dfe3e8] bg-[#f0f2f5] px-1.5 py-0.5 text-[10.5px] font-semibold text-[#2b3138]",
  strict:
    "rounded border border-[#f3dbd0] bg-[#fdf1ec] px-1.5 py-0.5 text-[10.5px] font-semibold text-[#9a3b1c]",
  custom:
    "rounded border border-[#e2dcf7] bg-[#f4f1fd] px-1.5 py-0.5 text-[10.5px] font-semibold text-[#4b3d94]",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function initialFontStep(scale: number): number {
  const found = FONT_STEPS.findIndex((item) => Math.abs(item.scale - scale) < 0.001);
  return found >= 0 ? found : 2;
}

export function SystemPanel() {
  const t = useT();
  const locale = useUiStore((state) => state.locale);
  const setLocale = useUiStore((state) => state.setLocale);
  const sendKey = useUiStore((state) => state.sendKey);
  const setSendKey = useUiStore((state) => state.setSendKey);
  const uiScale = useUiStore((state) => state.uiScale);
  const setUiScale = useUiStore((state) => state.setUiScale);
  const [step, setStep] = useState(() => initialFontStep(uiScale));
  const [tavily, setTavily] = useState("");
  const [brave, setBrave] = useState("");
  const [saving, setSaving] = useState(false);

  const current = FONT_STEPS[step];

  const saveSearch = async () => {
    if (!tavily.trim() && !brave.trim()) return;
    setSaving(true);
    try {
      await settingsApi.savePreferences({
        web_search: {
          ...(tavily.trim() ? { tavily_api_key: tavily.trim() } : {}),
          ...(brave.trim() ? { brave_api_key: brave.trim() } : {}),
        },
      });
      setTavily("");
      setBrave("");
      toast.success(t("settingsExt.webSearch.saved"));
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div data-testid="settings-panel-system" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">{t("settingsExt.system.title")}</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.system.subtitle")}</p>
      </div>

      <div className="border-chat-hairline flex items-center justify-between gap-3 border-b py-2">
        <div>
          <div className="text-xs">{t("settingsExt.system.language")}</div>
          <div className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.system.languageHint")}</div>
        </div>
        <select
          data-testid="settings-locale"
          value={locale}
          onChange={(event) => setLocale(event.target.value as typeof locale)}
          className="border-chat-hairline bg-card text-foreground rounded-md border px-2 py-1 text-xs"
        >
          <option value="zh">简体中文</option>
          <option value="en">English</option>
        </select>
      </div>

      <div className="border-chat-hairline flex items-center justify-between gap-3 border-b py-2">
        <div>
          <div className="text-xs">{t("settingsExt.system.sendKey")}</div>
          <div className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.system.sendKeyHint")}</div>
        </div>
        <div className="border-chat-hairline flex overflow-hidden rounded-md border">
          {(["enter", "ctrl+enter"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              data-testid={`settings-sendkey-${mode}`}
              aria-pressed={sendKey === mode}
              onClick={() => setSendKey(mode)}
              className={
                sendKey === mode
                  ? "bg-primary/5 px-2.5 py-1 text-[11px] font-medium"
                  : "text-chat-muted px-2.5 py-1 text-[11px]"
              }
            >
              {t(`settingsExt.system.sendKey_${mode === "enter" ? "enter" : "ctrl"}`)}
            </button>
          ))}
        </div>
      </div>

      <div className="py-2">
        <div className="text-xs">{t("settingsExt.system.fontSize")}</div>
        <input
          type="range"
          min={0}
          max={5}
          step={1}
          value={step}
          data-testid="settings-font-slider"
          aria-label={t("settingsExt.system.fontSize")}
          onChange={(event) => {
            const next = Number(event.target.value);
            setStep(next);
            setUiScale(FONT_STEPS[next].scale);
          }}
          className="accent-foreground mt-3 w-full cursor-pointer"
        />
        <div className="text-chat-muted flex justify-between px-0.5 text-[11px]">
          {FONT_STEPS.map((item) => (
            <span key={item.key}>{t(`settingsExt.system.font_${item.key}`)}</span>
          ))}
        </div>
        <div className="text-chat-muted mt-1.5 text-[11px]" data-testid="settings-font-current">
          {t("settingsExt.system.fontCurrent")}
          {t(`settingsExt.system.font_${current.key}`)}（{Math.round(current.scale * 100)}%）
        </div>
      </div>

      <div>
        <h3 className="text-chat-ink text-xs font-medium">WebSearch</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.webSearch.hint")}</p>
      </div>

      <div className="border-chat-hairline rounded-lg border p-2.5">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-medium">Tavily API Key</span>
          <a
            className="text-[11px] text-[#185FA5] hover:underline"
            href="https://app.tavily.com/home"
            target="_blank"
            rel="noreferrer"
          >
            {t("settingsExt.webSearch.getKey")} ↗
          </a>
        </div>
        <input
          type="text"
          value={tavily}
          data-testid="settings-tavily-key"
          placeholder="tvly-..."
          onChange={(event) => setTavily(event.target.value)}
          className="border-chat-hairline bg-card text-foreground mt-2 w-full rounded-md border px-2 py-1 font-mono text-[11px]"
        />
        <p className="text-chat-muted mt-1.5 text-[11px]">{t("settingsExt.webSearch.tavilyNote")}</p>
      </div>

      <div className="border-chat-hairline rounded-lg border p-2.5">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-medium">Brave Search API Key</span>
          <a
            className="text-[11px] text-[#185FA5] hover:underline"
            href="https://api.search.brave.com/app/keys"
            target="_blank"
            rel="noreferrer"
          >
            {t("settingsExt.webSearch.getKey")} ↗
          </a>
        </div>
        <input
          type="text"
          value={brave}
          data-testid="settings-brave-key"
          placeholder="Brave Search token"
          onChange={(event) => setBrave(event.target.value)}
          className="border-chat-hairline bg-card text-foreground mt-2 w-full rounded-md border px-2 py-1 font-mono text-[11px]"
        />
        <p className="text-chat-muted mt-1.5 text-[11px]">{t("settingsExt.webSearch.braveNote")}</p>
      </div>

      <div className="flex justify-end">
        <Button size="sm" disabled={saving} onClick={() => void saveSearch()}>
          {t("settingsExt.save")}
        </Button>
      </div>
    </div>
  );
}

export function PermissionsPanel() {
  const t = useT();
  const [prefs, setPrefs] = useState<PreferenceState | null>(null);
  const [rules, setRules] = useState<PermissionRule[]>([]);
  const [grants, setGrants] = useState<ApprovalGrant[]>([]);
  const [tab, setTab] = useState<PermissionTab>("presets");
  const [pending, setPending] = useState<PermissionPreset | null>(null);
  const [saving, setSaving] = useState(false);
  const [floorOpen, setFloorOpen] = useState(false);
  const [grantsOpen, setGrantsOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const [nextPrefs, nextRules, nextGrants] = await Promise.all([
        settingsApi.preferences(),
        settingsApi.permissionRules(),
        settingsApi.approvalGrants(),
      ]);
      setPrefs(nextPrefs);
      setRules(nextRules.rules ?? []);
      setGrants(nextGrants.grants ?? []);
    } catch (error) {
      toast.error(errorText(error));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const permission = prefs?.permission;
  const snapshot = permission ? snapshotOf(permission) : null;
  const activePreset = snapshot ? currentPresetKey(snapshot) : null;

  const applyPreset = async () => {
    if (!pending) return;
    setSaving(true);
    try {
      setPrefs(await settingsApi.savePreferences({ permission_preset: pending }));
      setPending(null);
      toast.success(t("settingsExt.permissions.applied"));
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const saveRules = async (next: PermissionRule[]) => {
    setSaving(true);
    try {
      const payload = next.map((rule) => ({ pattern: rule.pattern, action: rule.action }));
      setRules((await settingsApi.savePermissionRules(payload)).rules ?? []);
      toast.success(t("settingsExt.permissions.ruleSaved"));
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const revokeGrant = async (grant: ApprovalGrant) => {
    try {
      await settingsApi.revokeApprovalGrant({
        session_key: grant.session_key,
        tool_name: grant.tool_name,
        params_digest: grant.params_digest,
        scope: grant.scope,
      });
      setGrants((await settingsApi.approvalGrants()).grants ?? []);
      toast.success(t("settingsExt.permissions.grantRevoked"));
    } catch (error) {
      toast.error(errorText(error));
    }
  };

  const revokeAllGrants = async () => {
    try {
      await Promise.all(
        grants.map((grant) =>
          settingsApi.revokeApprovalGrant({
            session_key: grant.session_key,
            tool_name: grant.tool_name,
            params_digest: grant.params_digest,
            scope: grant.scope,
          }),
        ),
      );
      setGrants((await settingsApi.approvalGrants()).grants ?? []);
      toast.success(
        t("settingsExt.permissions.grantsRevokedAll").replace("{count}", String(grants.length)),
      );
    } catch (error) {
      toast.error(errorText(error));
    }
  };

  const tabs: Array<{ value: PermissionTab; label: string; count?: number }> = [
    { value: "presets", label: t("settingsExt.permissions.tabPresets") },
    { value: "rules", label: t("settingsExt.permissions.tabRules"), count: rules.length },
  ];

  return (
    <div data-testid="settings-panel-permissions" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">{t("settingsExt.permissions.title")}</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.permissions.subtitle")}</p>
      </div>

      {permission ? (
        <div
          data-testid="permission-status"
          className="border-chat-hairline rounded-xl border bg-[#fbfcfd] px-3 py-2.5"
        >
          <div className="flex items-center gap-2">
            <span className={PRESET_CHIP[activePreset ?? "custom"]}>
              {activePreset
                ? t(`settingsExt.permissions.preset_${activePreset}`)
                : t("settingsExt.permissions.statusCustom")}
            </span>
            {activePreset ? null : (
              <span className="text-chat-muted text-[10.5px]">
                {t("settingsExt.permissions.statusCustomNote")}
              </span>
            )}
            <button
              type="button"
              data-testid="permission-floor-btn"
              aria-expanded={floorOpen}
              onClick={() => setFloorOpen((value) => !value)}
              className="text-chat-muted hover:text-chat-ink ml-auto shrink-0 text-[10.5px]"
            >
              {t("settingsExt.permissions.floorAction")}
            </button>
          </div>

          <p className="text-chat-muted mt-1 text-[11px] leading-5">
            {t("settingsExt.permissions.statusAsk")}&nbsp;
            <b className="text-chat-ink font-mono">{permission.ask_risk_levels.join("、") || "—"}</b>
            {" · "}
            <b className="text-chat-ink">{permission.ask_tools.length}</b>&nbsp;
            {t("settingsExt.permissions.statusToolUnit")}
            {" · "}
            {t("settingsExt.permissions.statusBypass")}&nbsp;
            <b className="text-chat-ink">{permission.bypass_tools.length}</b>&nbsp;
            {t("settingsExt.permissions.statusBypassUnit")}
            {" · "}
            {t("settingsExt.permissions.statusGrants")}&nbsp;
            <b className="text-chat-ink">{grants.length}</b>&nbsp;
            {t("settingsExt.permissions.statusGrantsUnit")}
          </p>

          <p
            data-testid="permission-deny-line"
            className="mt-0.5 text-[10.5px] leading-5 text-[#A32D2D]"
          >
            {t("settingsExt.permissions.deny")}：{permission.deny_commands.length}&nbsp;
            {t("settingsExt.permissions.denyUnit")} · {t("settingsExt.permissions.denyFrozen")}
          </p>

          <p className="text-chat-muted/80 mt-0.5 text-[10px] leading-5">
            {t("settingsExt.permissions.statusScopeNote")}
          </p>

          {floorOpen ? (
            <div
              data-testid="permission-floor-sheet"
              className="border-chat-hairline mt-2 border-t pt-2"
            >
              <div className="flex flex-wrap gap-1.5">
                {permission.deny_commands.map((command) => (
                  <code
                    key={command}
                    className="rounded-md border border-[#f3d6d3] bg-[#fbeceb] px-1.5 py-0.5 font-mono text-[10.5px] text-[#8f2a2a]"
                  >
                    {command}
                  </code>
                ))}
              </div>
              <p className="text-chat-muted mt-2 text-[10px] leading-relaxed">
                {t("settingsExt.permissions.floorNote")}
              </p>
              <p className="text-chat-muted mt-0.5 text-[10px] leading-relaxed">
                {t("settingsExt.permissions.floorNoteExtra")}
              </p>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="border-chat-hairline flex items-end gap-4 border-b">
        {tabs.map((item, index) => (
          <button
            key={item.value}
            type="button"
            data-testid={`permission-tab-${item.value}`}
            onClick={() => setTab(item.value)}
            className={
              tab === item.value
                ? "text-chat-ink -mb-px border-b-2 border-current pb-2 text-[12.5px] font-semibold"
                : "text-chat-muted hover:text-chat-ink -mb-px border-b-2 border-transparent pb-2 text-[12.5px]"
            }
          >
            {index > 0 ? <span className="text-chat-muted/30 mr-4">|</span> : null}
            {item.label}
            {item.count === undefined ? null : (
              <span className="text-chat-muted ml-1.5 text-[11px] font-normal">{item.count}</span>
            )}
          </button>
        ))}
      </div>

      {tab === "presets" ? (
        <PermissionPresetList
          snapshot={snapshot}
          activePreset={activePreset}
          pending={pending}
          onPick={setPending}
        />
      ) : (
        <div className="space-y-2">
          <PermissionRulesSection
            rules={rules}
            saving={saving}
            onSave={(next) => void saveRules(next)}
          />
          <div className="border-chat-hairline mt-3 overflow-hidden rounded-xl border">
            <button
              type="button"
              data-testid="permission-grants-fold"
              aria-expanded={grantsOpen}
              onClick={() => setGrantsOpen((value) => !value)}
              className="flex w-full items-center gap-2 bg-[#fbfcfd] px-3 py-2 text-left"
            >
              <span className="text-chat-ink shrink-0 text-[11.5px] font-semibold">
                {t("settingsExt.permissions.grantsFold")}
                <span className="text-chat-muted ml-1.5 text-[10px] font-normal">
                  {grants.length}
                </span>
              </span>
              <span className="text-chat-muted min-w-0 flex-1 truncate text-[10px]">
                {t("settingsExt.permissions.grantsFoldHint")}
              </span>
              <ChevronDown
                className={
                  grantsOpen
                    ? "text-chat-muted size-3 shrink-0 rotate-180 transition-transform"
                    : "text-chat-muted size-3 shrink-0 transition-transform"
                }
                aria-hidden="true"
              />
            </button>
            {grantsOpen ? (
              <div className="border-chat-hairline border-t px-3 pt-2 pb-3">
                <ApprovalGrantsSection grants={grants} onRevoke={revokeGrant} onRevokeAll={revokeAllGrants} />
              </div>
            ) : null}
          </div>
        </div>
      )}

      {pending ? (
        <div
          data-testid="permission-savebar"
          className="border-chat-hairline bg-card sticky bottom-0 flex items-center gap-2 border-t pt-2"
        >
          <p className="text-chat-muted flex-1 text-[11.5px]">
            {t("settingsExt.permissions.savePending").replace(
              "{name}",
              t(`settingsExt.permissions.preset_${pending}`),
            )}
          </p>
          <Button variant="outline" size="sm" onClick={() => setPending(null)}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" disabled={saving} onClick={() => void applyPreset()}>
            {t("settingsExt.permissions.apply")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

interface PermissionPresetListProps {
  snapshot: PermissionSnapshot | null;
  activePreset: PermissionPreset | null;
  pending: PermissionPreset | null;
  onPick: (preset: PermissionPreset) => void;
}

/** 功能：档位三卡——每张卡画出相对当前快照的增减项。 */
function PermissionPresetList({ snapshot, activePreset, pending, onPick }: PermissionPresetListProps) {
  const t = useT();

  return (
    <div className="space-y-2">
      <p className="text-chat-muted text-[10.5px]">{t("settingsExt.permissions.diffHint")}</p>
      {PRESET_ORDER.map((preset) => {
        const diff = snapshot ? presetDiff(preset, snapshot) : null;
        const selected = pending === preset;
        return (
          <button
            key={preset}
            type="button"
            data-testid={`permission-preset-${preset}`}
            data-active={selected}
            onClick={() => onPick(preset)}
            className={
              selected
                ? "border-foreground relative w-full rounded-lg border p-2.5 text-left ring-1 ring-foreground"
                : "border-chat-hairline relative w-full rounded-lg border p-2.5 text-left"
            }
          >
            <div className="flex items-center gap-2">
              <span className="text-chat-ink text-xs font-medium">
                {t(`settingsExt.permissions.preset_${preset}`)}
              </span>
              {activePreset === preset ? (
                <span className="text-chat-muted border-chat-hairline rounded border px-1 text-[9.5px]">
                  {t("settingsExt.permissions.currentTag")}
                </span>
              ) : null}
            </div>
            <p className="text-chat-muted mt-0.5 pr-6 text-[11px] leading-5">
              {t(`settingsExt.permissions.presetHint_${preset}`)}
            </p>
            <div className="mt-1.5 flex flex-wrap gap-1">
              <PermissionDiffChips diff={diff} />
            </div>
          </button>
        );
      })}
    </div>
  );
}

/** 功能：差异徽标（多问 / 少问 / 免询问增减）。 */
function PermissionDiffChips({ diff }: { diff: PresetDiff | null }) {
  const t = useT();
  if (!diff) return null;
  if (isEmptyDiff(diff)) {
    return <span className="rounded bg-[#f5f6f8] px-1.5 py-0.5 text-[10px] text-[#9aa1ab]">{t("settingsExt.permissions.diffSame")}</span>;
  }

  const chips: Array<{ key: string; tone: "add" | "drop"; text: string }> = [];
  if (diff.riskAdd.length) {
    chips.push({
      key: "risk-add",
      tone: "add",
      text: t("settingsExt.permissions.diffRiskAdd").replace("{levels}", diff.riskAdd.join("、")),
    });
  }
  if (diff.riskDel.length) {
    chips.push({
      key: "risk-del",
      tone: "drop",
      text: t("settingsExt.permissions.diffRiskDel").replace("{levels}", diff.riskDel.join("、")),
    });
  }
  if (diff.toolAdd.length) {
    chips.push({
      key: "tool-add",
      tone: "add",
      text: t("settingsExt.permissions.diffToolAdd").replace("{count}", String(diff.toolAdd.length)),
    });
  }
  if (diff.toolDel.length) {
    chips.push({
      key: "tool-del",
      tone: "drop",
      text: t("settingsExt.permissions.diffToolDel").replace("{count}", String(diff.toolDel.length)),
    });
  }
  if (diff.bypassAdd.length) {
    chips.push({
      key: "bypass-add",
      tone: "add",
      text: t("settingsExt.permissions.diffBypassAdd").replace("{count}", String(diff.bypassAdd.length)),
    });
  }
  if (diff.bypassDel.length) {
    chips.push({
      key: "bypass-del",
      tone: "drop",
      text: t("settingsExt.permissions.diffBypassDel").replace("{count}", String(diff.bypassDel.length)),
    });
  }

  return (
    <>
      {chips.map((chip) => (
        <span
          key={chip.key}
          className={
            chip.tone === "add"
              ? "rounded bg-[#f2f6ff] px-1.5 py-0.5 text-[10px] text-[#2f5298]"
              : "rounded bg-[#f7f8fa] px-1.5 py-0.5 text-[10px] text-[#6b7280]"
          }
        >
          {chip.text}
        </span>
      ))}
    </>
  );
}

interface PermissionRulesSectionProps {
  rules: PermissionRule[];
  saving: boolean;
  onSave: (next: PermissionRule[]) => void;
}

/** 功能：权限规则——常驻优先级说明 + 规则列表 + 添加行。 */
function PermissionRulesSection({ rules, saving, onSave }: PermissionRulesSectionProps) {
  const t = useT();
  const [pattern, setPattern] = useState("");
  const [action, setAction] = useState<PermissionRuleAction>("ask");

  const add = () => {
    const value = pattern.trim();
    if (!value) return;
    onSave([...rules, { pattern: value, action, kind: "", tool: "", content: "", source: "user" }]);
    setPattern("");
  };

  return (
    <div data-testid="permission-rules-panel" className="space-y-2">
      <div
        data-testid="permission-priority"
        className="border-chat-hairline rounded-xl border bg-[#fbfcfd] px-3 py-2"
      >
        <div className="flex flex-wrap items-center gap-1 text-[11px] leading-5">
          <span className="text-chat-muted">{t("settingsExt.permissions.prioTitle")}</span>
          <span className="text-chat-muted/50">›</span>
          <b className="text-chat-ink">{t("settingsExt.permissions.prioDenyRule")}</b>
          <span className="text-chat-muted/50">›</span>
          <b className="text-chat-ink">{t("settingsExt.permissions.prioFloor")}</b>
          <span className="text-chat-muted/50">›</span>
          <b className="text-chat-ink">{t("settingsExt.permissions.prioAllowRule")}</b>
          <span className="text-chat-muted/50">›</span>
          <b className="text-chat-ink">{t("settingsExt.permissions.prioPreset")}</b>
          <span className="text-chat-muted/50">›</span>
          <b className="text-chat-ink">{t("settingsExt.permissions.prioRisk")}</b>
        </div>
        <p className="mt-1 flex gap-1 text-[10px] leading-relaxed text-[#9a3b1c]">
          <span className="shrink-0">!</span>
          <span>{t("settingsExt.permissions.prioNote")}</span>
        </p>
      </div>

      <div className="space-y-1" data-testid="permission-rules-list">
        {rules.length === 0 ? (
          <p className="text-chat-muted text-[11px]">{t("settingsExt.permissions.rulesEmpty")}</p>
        ) : (
          rules.map((rule) => (
            <div
              key={`${rule.action}:${rule.pattern}`}
              className="border-chat-hairline flex items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[11px]"
            >
              <span
                className={
                  rule.action === "deny"
                    ? "rounded bg-[#A32D2D]/10 px-1.5 py-0.5 font-mono text-[9.5px] text-[#A32D2D]"
                    : rule.action === "ask"
                      ? "rounded bg-amber-500/10 px-1.5 py-0.5 font-mono text-[9.5px] text-amber-700"
                      : "rounded bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[9.5px] text-emerald-700"
                }
              >
                {rule.action}
              </span>
              <span className="text-chat-ink min-w-0 flex-1 truncate font-mono text-[11px]">
                {rule.pattern}
              </span>
              <span className="text-chat-muted/80 shrink-0 text-[10px]">
                {rule.source ? t(`settingsExt.permissions.source_${rule.source}`) : ""}
              </span>
              <button
                type="button"
                data-testid={`permission-rule-delete-${rule.action}-${rule.pattern}`}
                aria-label={`${t("settingsExt.permissions.deleteRule")} ${rule.pattern}`}
                className="text-chat-muted/60 hover:text-destructive shrink-0 px-1 text-[13px] leading-none"
                onClick={() =>
                  onSave(
                    rules.filter(
                      (item) => !(item.pattern === rule.pattern && item.action === rule.action),
                    ),
                  )
                }
              >
                ×
              </button>
            </div>
          ))
        )}
      </div>

      <div className="flex gap-2">
        <select
          data-testid="permission-rule-action"
          value={action}
          onChange={(event) => setAction(event.target.value as PermissionRuleAction)}
          className="border-chat-hairline bg-card text-chat-ink h-8 rounded-md border px-2 font-mono text-[11.5px]"
        >
          <option value="allow">allow</option>
          <option value="ask">ask</option>
          <option value="deny">deny</option>
        </select>
        <input
          data-testid="permission-rule-pattern"
          value={pattern}
          onChange={(event) => setPattern(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") add();
          }}
          placeholder="bash:npm:* / read / write:src/**"
          className="border-chat-hairline bg-card text-chat-ink h-8 min-w-0 flex-1 rounded-md border px-2 font-mono text-[11px]"
        />
        <Button size="sm" disabled={saving || !pattern.trim()} onClick={add}>
          {t("settingsExt.permissions.addRule")}
        </Button>
      </div>

      <p className="text-chat-muted/80 text-[10px] leading-relaxed">
        {t("settingsExt.permissions.syntaxHint")}
      </p>
    </div>
  );
}

interface ApprovalGrantsSectionProps {
  grants: ApprovalGrant[];
  onRevoke: (grant: ApprovalGrant) => void;
  onRevokeAll: () => void;
}

/** 功能：临时放行列表——标出「整个工具」与到期时间，可逐条或全部撤销。 */
function ApprovalGrantsSection({ grants, onRevoke, onRevokeAll }: ApprovalGrantsSectionProps) {
  const t = useT();

  return (
    <div data-testid="approval-grants-panel" className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-chat-muted text-[10.5px]">
          {t("settingsExt.permissions.grantsCount").replace("{count}", String(grants.length))}
        </span>
        {grants.length > 0 ? (
          <button
            type="button"
            data-testid="approval-grants-revoke-all"
            onClick={onRevokeAll}
            className="text-chat-muted hover:text-destructive text-[10.5px]"
          >
            {t("settingsExt.permissions.grantsRevokeAll")}
          </button>
        ) : null}
      </div>

      {grants.length === 0 ? (
        <p className="text-chat-muted text-[11px]">{t("settingsExt.permissions.grantsEmpty")}</p>
      ) : (
        grants.map((grant) => (
          <div
            key={`${grant.scope}:${grant.session_key}:${grant.tool_name}:${grant.params_digest}`}
            className="border-chat-hairline flex items-center gap-2 rounded-lg border px-2.5 py-1.5"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="text-chat-ink font-mono text-[11.5px] font-medium">
                  {grant.tool_name}
                </span>
                <span className="text-chat-muted rounded bg-[#f2f4f6] px-1.5 py-0.5 text-[9.5px]">
                  {grant.scope === "session"
                    ? t("settingsExt.permissions.scopeSession")
                    : t("settingsExt.permissions.scopeOnce")}
                </span>
                <span className="rounded bg-[#fdf5f2] px-1.5 py-0.5 text-[9.5px] text-[#9a3b1c]">
                  {grant.scope === "session"
                    ? t("settingsExt.permissions.grantToolWide")
                    : t("settingsExt.permissions.grantSingleCall")}
                </span>
              </div>
              <p className="text-chat-muted/80 mt-0.5 truncate text-[10px]">
                {t("settingsExt.permissions.grantSession").replace(
                  "{id}",
                  `${grant.session_key.slice(0, 8)}…`,
                )}
              </p>
            </div>
            <div className="shrink-0 text-right">
              <p className="text-chat-muted text-[10px] tabular-nums">
                {t("settingsExt.permissions.grantExpires").replace("{time}", expiryText(grant.expires_at))}
              </p>
              <button
                type="button"
                data-testid={`approval-grant-revoke-${grant.tool_name}`}
                className="text-chat-muted hover:text-destructive text-[10.5px]"
                onClick={() => onRevoke(grant)}
              >
                {t("settingsExt.permissions.revoke")}
              </button>
            </div>
          </div>
        ))
      )}

      <p className="text-chat-muted/80 border-chat-hairline border-t border-dashed pt-2 text-[10px] leading-relaxed">
        {t("settingsExt.permissions.grantsNote")}
      </p>
    </div>
  );
}

/** 功能：到期时刻（本地时间 HH:mm）——接口给的是 unix 秒。 */
function expiryText(expiresAt: number): string {
  if (!expiresAt) return "—";
  return new Date(expiresAt * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function DataPanel() {
  const t = useT();
  const [storage, setStorage] = useState<StorageInfo | null>(null);
  const [days, setDays] = useState(90);
  const [preview, setPreview] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [next, prefs] = await Promise.all([settingsApi.storage(), settingsApi.preferences()]);
      setStorage(next);
      setDays(prefs.retention_days || 90);
    } catch (error) {
      toast.error(errorText(error));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const disk = storage?.disk;
  const usedRatio = disk && disk.total > 0 ? disk.used / disk.total : 0;
  const selfRatio = disk && disk.total > 0 ? (storage?.total_bytes ?? 0) / disk.total : 0;

  const runPreview = async () => {
    setBusy(true);
    try {
      const result = await settingsApi.retentionPreview(days);
      setPreview(result.sessions);
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const runCleanup = async () => {
    if (preview === null || preview === 0) return;
    setBusy(true);
    try {
      const result = await settingsApi.retentionCleanup(days);
      toast.success(t("settingsExt.data.deleted").replace("{n}", String(result.deleted)));
      setPreview(null);
      await load();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="settings-panel-data" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">{t("settingsExt.data.title")}</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.data.subtitle")}</p>
      </div>

      <div className="border-chat-hairline border-b py-2">
        <div className="text-xs">{t("settingsExt.data.dir")}</div>
        <div className="text-chat-muted mt-1 font-mono text-[11px]">{storage?.data_dir ?? "—"}</div>
      </div>

      <div className="py-2">
        <div className="text-xs">{t("settingsExt.data.disk")}</div>
        <div
          data-testid="settings-disk-bar"
          className="border-chat-hairline mt-2 flex h-2.5 overflow-hidden rounded-full border bg-[#f1efe8]"
        >
          <span
            className="bg-[#18181a]"
            style={{ width: `${Math.max(selfRatio * 100, 0.4)}%`, minWidth: 3 }}
            title={formatBytes(storage?.total_bytes ?? 0)}
          />
          <span className="bg-[#b4b2a9]" style={{ width: `${usedRatio * 100}%` }} />
        </div>
        <div className="text-chat-muted mt-1.5 flex flex-wrap gap-3 text-[11px]">
          <span>
            {t("settingsExt.data.self")} {formatBytes(storage?.total_bytes ?? 0)}
          </span>
          <span>
            {t("settingsExt.data.used")} {formatBytes(disk?.used ?? 0)} /{" "}
            {formatBytes(disk?.total ?? 0)}
          </span>
          <span>
            {t("settingsExt.data.free")} {formatBytes(disk?.free ?? 0)}
          </span>
        </div>
      </div>

      <div>
        <p className="text-chat-muted text-[11px]">{t("settingsExt.data.dbs")}</p>
        <table className="mt-1 w-full text-[11px]">
          <tbody>
            {(storage?.dbs ?? []).map((db) => (
              <tr key={db.name} className="border-chat-hairline border-b last:border-b-0">
                <td className="py-1 font-mono">{db.name}</td>
                <td className="py-1 text-right">{formatBytes(db.bytes)}</td>
                <td className="text-chat-muted py-1 text-right">{db.mtime.replace("T", " ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="py-2">
        <div className="text-xs">{t("settingsExt.data.retention")}</div>
        <div className="mt-2 flex items-center gap-2">
          {[30, 90, 365].map((value) => (
            <button
              key={value}
              type="button"
              data-testid={`retention-${value}`}
              aria-pressed={days === value}
              onClick={() => {
                setDays(value);
                setPreview(null);
              }}
              className={
                days === value
                  ? "bg-primary/5 rounded-md border border-chat-hairline px-2.5 py-1 text-[11px] font-medium"
                  : "text-chat-muted rounded-md border border-chat-hairline px-2.5 py-1 text-[11px]"
              }
            >
              {value}
              {t("settingsExt.data.days")}
            </button>
          ))}
          <span className="text-chat-muted ml-auto text-[11px]">
            {preview === null
              ? t("settingsExt.data.notPreviewed")
              : t("settingsExt.data.previewResult").replace("{n}", String(preview))}
          </span>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void runPreview()}>
            {t("settingsExt.data.preview")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            data-testid="retention-cleanup"
            disabled={busy || !preview}
            onClick={() => void runCleanup()}
          >
            {t("settingsExt.data.cleanup")}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function McpPanel() {
  const t = useT();
  const [payload, setPayload] = useState<McpServersPayload | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPayload(await settingsApi.mcpServers());
    } catch (error) {
      toast.error(errorText(error));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (server: McpServer) => {
    try {
      await settingsApi.patchMcpServer(server.name, { enabled: !server.enabled });
      await load();
    } catch (error) {
      toast.error(errorText(error));
    }
  };

  const test = async (server: McpServer) => {
    setTesting(server.name);
    try {
      const result = await settingsApi.testMcpServer(server.name);
      if (result.ok) {
        toast.success(
          t("settingsExt.mcp.testOk").replace("{n}", String(result.tool_count)),
        );
      } else {
        toast.error(`${t("settingsExt.mcp.testFail")}：${result.error}`);
      }
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setTesting(null);
    }
  };

  return (
    <div data-testid="settings-panel-mcp" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">{t("settingsExt.mcp.title")}</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.mcp.subtitle")}</p>
      </div>

      {payload ? (
        <div className="border-chat-hairline rounded-lg border p-2.5">
          <div className="flex items-center justify-between gap-3">
            <span className="text-xs">{t("settingsExt.mcp.autostart")}</span>
            <Switch
              testId="mcp-global-autostart"
              checked={payload.global.autostart}
              onChange={async (next) => {
                await settingsApi.saveMcpGlobal({ autostart: next });
                await load();
              }}
            />
          </div>
          <div className="border-chat-hairline mt-2 flex items-center justify-between gap-3 border-t pt-2">
            <span className="text-xs">{t("settingsExt.mcp.lazy")}</span>
            <Switch
              testId="mcp-global-lazy"
              checked={payload.global.lazy_exposure}
              onChange={async (next) => {
                await settingsApi.saveMcpGlobal({ lazy_exposure: next });
                await load();
              }}
            />
          </div>
        </div>
      ) : (
        <p className="text-chat-muted text-xs">{t("settingsExt.loading")}</p>
      )}

      <div className="space-y-2">
        {(payload?.servers ?? []).map((server) => (
          <div
            key={server.name}
            data-testid={`mcp-server-${server.name}`}
            className="border-chat-hairline rounded-lg border p-2.5"
          >
            <div className="flex items-center gap-2">
              <span
                className={
                  server.status === "connected"
                    ? "size-1.5 rounded-full bg-[#1D9E75]"
                    : server.status === "disabled"
                      ? "size-1.5 rounded-full bg-[#B4B2A9]"
                      : "size-1.5 rounded-full bg-[#E24B4A]"
                }
              />
              <span className="text-xs font-medium">{server.name}</span>
              {server.category ? (
                <span className="border-chat-hairline text-chat-muted rounded-full border px-1.5 text-[10px]">
                  {server.category}
                </span>
              ) : null}
              <span className="text-chat-muted ml-auto text-[11px]">
                {server.transport} · {server.tool_count} {t("settingsExt.mcp.toolsUnit")}
              </span>
              {server.homepage ? (
                <a
                  href={server.homepage}
                  target="_blank"
                  rel="noreferrer"
                  className="text-chat-muted hover:text-chat-ink"
                  aria-label={t("settingsExt.mcp.homepage")}
                >
                  <ArrowUpRight className="size-3.5" />
                </a>
              ) : null}
              <Switch
                testId={`mcp-switch-${server.name}`}
                checked={server.enabled}
                onChange={() => void toggle(server)}
              />
            </div>

            <div className="mt-2 flex items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                data-testid={`mcp-test-${server.name}`}
                disabled={testing === server.name || !server.enabled}
                onClick={() => void test(server)}
              >
                <Cable className="size-3" /> {t("settingsExt.mcp.test")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => setExpanded(expanded === server.name ? null : server.name)}
              >
                {expanded === server.name
                  ? t("settingsExt.mcp.collapse")
                  : t("settingsExt.mcp.detail")}
              </Button>
              {server.enabled ? null : (
                <span className="text-chat-muted text-[11px]">{t("settingsExt.mcp.disabledHint")}</span>
              )}
            </div>

            {expanded === server.name ? (
              <div className="border-chat-hairline mt-2 border-t pt-2 text-[11px]">
                <div className="text-chat-muted truncate font-mono">
                  {server.transport === "stdio" ? server.command : server.url}
                </div>
                <div className="mt-1">
                  <span className="text-chat-muted">
                    {t("settingsExt.mcp.allowlist")}：
                  </span>
                  <span className="font-mono">
                    {server.allowed_tools === null
                      ? t("settingsExt.mcp.unrestricted")
                      : server.allowed_tools.join(", ") || t("settingsExt.mcp.denyAll")}
                  </span>
                </div>
                <div className="text-chat-muted mt-1 font-mono">
                  {server.tools.length ? server.tools.join(", ") : t("settingsExt.mcp.noTools")}
                </div>
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </div>
  );
}

export function CapabilitiesPanel() {
  const t = useT();
  const [tab, setTab] = useState<"tools" | "skills">("tools");
  const [tools, setTools] = useState<ToolMeta[]>([]);
  const [skills, setSkills] = useState<SkillMeta[]>([]);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("");

  useEffect(() => {
    void (async () => {
      try {
        const response = await settingsApi.toolsMeta();
        // 破坏者防线：/tools/meta 返回数组；若误当对象取 .tools 会静默变空表
        setTools(Array.isArray(response) ? response : []);
      } catch {
        setTools([]);
      }
      try {
        const response = await settingsApi.skillsList();
        setSkills(response.skills ?? []);
      } catch {
        setSkills([]);
      }
    })();
  }, []);

  const groups = Array.from(
    new Set(tools.map((tool) => tool.category).filter(Boolean)),
  ) as string[];
  const keyword = query.trim().toLowerCase();
  const filteredTools = tools.filter((tool) => {
    if (group && tool.category !== group) return false;
    if (!keyword) return true;
    return `${tool.name} ${tool.category ?? ""} ${tool.feature_group ?? ""}`
      .toLowerCase()
      .includes(keyword);
  });
  const filteredSkills = skills.filter((skill) =>
    keyword
      ? `${skill.name} ${skill.description ?? ""} ${(skill.tags ?? []).join(" ")}`
          .toLowerCase()
          .includes(keyword)
      : true,
  );

  const riskDot = (risk?: string) => {
    if (risk === "critical") return "bg-[#A32D2D]";
    if (risk === "high") return "bg-[#E24B4A]";
    if (risk === "medium") return "bg-[#EF9F27]";
    return "bg-[#9C9A92]";
  };

  const total = tab === "tools" ? filteredTools.length : filteredSkills.length;
  const all = tab === "tools" ? tools.length : skills.length;

  return (
    <div data-testid="settings-panel-capabilities" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">{t("settingsExt.caps.title")}</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.caps.subtitle")}</p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="border-chat-hairline flex w-max overflow-hidden rounded-md border">
          {(["tools", "skills"] as const).map((item) => (
            <button
              key={item}
              type="button"
              data-testid={`caps-tab-${item}`}
              aria-pressed={tab === item}
              onClick={() => setTab(item)}
              className={
                tab === item
                  ? "bg-primary/5 px-2.5 py-1 text-[11px] font-medium"
                  : "text-chat-muted px-2.5 py-1 text-[11px]"
              }
            >
              {t(`settingsExt.caps.tab_${item}`)}
            </button>
          ))}
        </div>
        <input
          data-testid="caps-search"
          value={query}
          spellCheck={false}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("settingsExt.caps.search")}
          className="border-chat-hairline bg-card text-chat-ink focus-visible:border-foreground h-7 min-w-0 flex-1 rounded-md border px-2 text-[11px] outline-none"
        />
        {tab === "tools" ? (
          <select
            data-testid="caps-group"
            value={group}
            onChange={(event) => setGroup(event.target.value)}
            className="border-chat-hairline bg-card text-chat-ink h-7 rounded-md border px-1.5 text-[11px] outline-none"
          >
            <option value="">{t("settingsExt.caps.allGroups")}</option>
            {groups.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        ) : null}
      </div>

      <p data-testid="caps-count" className="text-chat-muted text-[11px] tabular-nums">
        {t("settingsExt.caps.count")
          .replace("{shown}", String(total))
          .replace("{total}", String(all))}
      </p>

      {tab === "tools" ? (
        <div className="border-chat-hairline max-h-[46vh] overflow-y-auto rounded-md border">
          <table className="w-full text-[11px]">
            <tbody>
              {filteredTools.map((tool) => (
                <tr
                  key={tool.name}
                  data-testid="caps-tool-row"
                  className="border-chat-hairline border-b last:border-b-0"
                >
                  <td className="py-1 pl-2 font-mono">
                    <span className="flex items-center gap-1.5">
                      <span className={`size-1.5 shrink-0 rounded-full ${riskDot(tool.risk_level)}`} />
                      <span className="truncate">{tool.name}</span>
                      {tool.mcp ? (
                        <span className="text-chat-muted rounded bg-black/5 px-1 text-[10px]">
                          MCP
                        </span>
                      ) : null}
                    </span>
                  </td>
                  <td className="text-chat-muted py-1">{tool.category ?? "—"}</td>
                  <td className="text-chat-muted py-1 pr-2 text-right">{tool.risk_level ?? "—"}</td>
                </tr>
              ))}
              {filteredTools.length === 0 ? (
                <tr>
                  <td colSpan={3} className="text-chat-muted px-2 py-3 text-center">
                    {t("settingsExt.caps.noMatch")}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      ) : (
        <ul className="border-chat-hairline max-h-[46vh] overflow-y-auto rounded-md border text-[11px]">
          {filteredSkills.map((skill) => (
            <li
              key={skill.name}
              className="border-chat-hairline flex gap-2 border-b px-2 py-1 last:border-b-0"
            >
              <span className="flex-1 truncate font-mono">{skill.name}</span>
              <span className="text-chat-muted shrink-0 truncate">
                {(skill.tags ?? []).length > 0
                  ? (skill.tags ?? []).join(" · ")
                  : skill.always
                    ? t("settingsExt.caps.always")
                    : "—"}
              </span>
            </li>
          ))}
          {filteredSkills.length === 0 ? (
            <li className="text-chat-muted px-2 py-3 text-center">{t("settingsExt.caps.noMatch")}</li>
          ) : null}
        </ul>
      )}

      <p className="text-chat-muted text-[11px]">{t("settingsExt.caps.readonly")}</p>
    </div>
  );
}

export function ComputerUsePanel() {
  const t = useT();
  const [status, setStatus] = useState<ComputerStatus | null>(null);
  const [interpreter, setInterpreter] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await settingsApi.computerStatus();
      setStatus(next);
      setInterpreter((prev) => prev || next.interpreter.path || "");
    } catch (error) {
      toast.error(errorText(error));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const phase = status?.install.phase ?? "idle";
  const installing = ["preparing", "creating_env", "installing", "verifying"].includes(phase);
  const ready = Boolean(status?.deps_installed);
  const enabled = Boolean(status?.enabled);

  useEffect(() => {
    // 功能：安装期间轮询状态（进度/日志/失败原因），装完自动停
    if (!installing) return;
    const timer = window.setInterval(() => {
      void load();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [installing, load]);

  const applyEnabled = async (next: boolean) => {
    setBusy(true);
    try {
      const result = await settingsApi.setComputerEnabled(next, next);
      setStatus(result.status);
      setConfirming(false);
      // 功能：通知顶栏常驻提示条即时同步（不用轮询）
      window.dispatchEvent(
        new CustomEvent("bandi:computer-use-changed", { detail: { enabled: next } }),
      );
      toast.success(next ? t("settingsExt.cu.enabledToast") : t("settingsExt.cu.disabledToast"));
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (next: boolean) => {
    if (next && !ready) {
      toast.error(t("settingsExt.cu.needRuntime"));
      return;
    }
    if (next) {
      setConfirming(true);
      return;
    }
    await applyEnabled(false);
  };

  const applyInterpreter = async () => {
    setBusy(true);
    try {
      const result = await settingsApi.setComputerInterpreter(interpreter.trim());
      setStatus((prev) => (prev ? { ...prev, interpreter: result.interpreter } : prev));
      toast.success(t("settingsExt.cu.interpreterSaved"));
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const startInstall = async () => {
    setBusy(true);
    try {
      await settingsApi.installComputerRuntime(interpreter.trim());
      await load();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const deps = Object.entries(status?.deps ?? {});
  const logLines = (status?.install.log ?? []).slice(-3);

  return (
    <div data-testid="settings-panel-computer" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">Computer Use</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.cu.subtitle")}</p>
      </div>

      {enabled ? (
        <div
          data-testid="cu-banner"
          className="flex items-center justify-between gap-2 rounded-lg border border-[#FAC775] bg-[#FAEEDA] px-2.5 py-2 text-[11px] text-[#854F0B]"
        >
          <span className="font-medium">{t("settingsExt.cu.banner")}</span>
          <Button
            size="sm"
            variant="outline"
            data-testid="cu-banner-close"
            disabled={busy}
            onClick={() => void applyEnabled(false)}
          >
            {t("settingsExt.cu.disableNow")}
          </Button>
        </div>
      ) : null}

      <div className="border-chat-hairline space-y-2.5 rounded-lg border p-2.5 text-[11px]">
        <div className="flex items-center justify-between">
          <span>{t("settingsExt.cu.enable")}</span>
          <Switch
            testId="cu-enable"
            checked={enabled}
            disabled={busy || installing || (!ready && !enabled)}
            onChange={(next) => toggle(next)}
          />
        </div>
        {confirming ? (
          <div
            data-testid="cu-confirm"
            className="border-chat-hairline space-y-2 rounded-md border border-dashed p-2"
          >
            <p className="leading-5">{t("settingsExt.cu.confirmBody")}</p>
            <div className="flex gap-2">
              <Button
                size="sm"
                data-testid="cu-confirm-yes"
                disabled={busy}
                onClick={() => void applyEnabled(true)}
              >
                {t("settingsExt.cu.confirmYes")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>
                {t("settingsExt.cu.confirmCancel")}
              </Button>
            </div>
          </div>
        ) : null}

        <div className="flex items-start gap-2">
          <span
            className={`mt-1 size-1.5 shrink-0 rounded-full ${status?.interpreter.version ? "bg-[#1D9E75]" : "bg-[#E24B4A]"}`}
          />
          <span className="min-w-0 flex-1">
            <span className="block">{t("settingsExt.cu.pythonReady")}</span>
            <span className="text-chat-muted block truncate font-mono">
              {status?.interpreter.version
                ? `${status.interpreter.version} · ${status.interpreter.path}`
                : t("settingsExt.cu.pythonMissing")}
            </span>
          </span>
        </div>

        <div className="flex items-start gap-2">
          <span
            className={`mt-1 size-1.5 shrink-0 rounded-full ${ready ? "bg-[#1D9E75]" : "bg-[#E24B4A]"}`}
          />
          <span className="min-w-0 flex-1">
            <span className="block">{t("settingsExt.cu.runtimeEnv")}</span>
            <span className="text-chat-muted block truncate font-mono">{status?.venv_dir ?? "—"}</span>
          </span>
        </div>

        <div className="flex items-start gap-2" data-testid="cu-deps">
          <span
            className={`mt-1 size-1.5 shrink-0 rounded-full ${ready ? "bg-[#1D9E75]" : "bg-[#E24B4A]"}`}
          />
          <span className="min-w-0 flex-1">
            <span className="block">
              {ready ? t("settingsExt.cu.depsReady") : t("settingsExt.cu.depsMissing")}
            </span>
            <span className="text-chat-muted flex flex-wrap gap-x-2 gap-y-1 font-mono">
              {deps.length === 0 ? (
                <span>—</span>
              ) : (
                deps.map(([name, ok]) => (
                  <span key={name} className={ok ? "" : "text-[#E24B4A]"}>
                    {ok ? "✓" : "✗"} {name}
                  </span>
                ))
              )}
            </span>
          </span>
        </div>

        <div data-testid="cu-screen" className="flex items-center justify-between gap-2">
          <span>{t("settingsExt.cu.screen")}</span>
          <span className="text-chat-muted tabular-nums">
            {status?.screen?.width
              ? `${status.screen.width}×${status.screen.height}${
                  status.scale && status.scale !== 1
                    ? ` · ${Math.round(status.scale * 100)}%`
                    : ""
                }`
              : "—"}
          </span>
        </div>

        {installing || phase === "ready" || phase === "failed" ? (
          <div className="space-y-1.5">
            <div
              data-testid="cu-progress"
              className="border-chat-hairline h-1.5 overflow-hidden rounded-full border bg-[#f1efe8]"
            >
              <span
                className={`block h-full rounded-full transition-all ${phase === "failed" ? "bg-[#E24B4A]" : "bg-foreground"}`}
                style={{
                  width: `${Math.min(100, Math.max(phase === "failed" ? 100 : 2, status?.install.progress ?? 0))}%`,
                }}
              />
            </div>
            <p className="text-chat-muted">
              {phase === "failed"
                ? status?.install.error || t("settingsExt.cu.installFailed")
                : status?.install.message || t("settingsExt.cu.installIdle")}
            </p>
            {phase === "failed" && logLines.length > 0 ? (
              <pre className="text-chat-muted max-h-24 overflow-y-auto rounded bg-black/5 p-1.5 font-mono text-[10px] leading-4 whitespace-pre-wrap">
                {logLines.join("\n")}
              </pre>
            ) : null}
          </div>
        ) : null}

        <Button
          size="sm"
          variant="outline"
          data-testid="cu-install"
          disabled={busy || installing}
          onClick={() => void startInstall()}
        >
          {installing
            ? t("settingsExt.cu.installing")
            : ready
              ? t("settingsExt.cu.reinstall")
              : t("settingsExt.cu.install")}
        </Button>
      </div>

      <div className="border-chat-hairline space-y-2 rounded-lg border p-2.5">
        <div className="text-xs">{t("settingsExt.cu.interpreterTitle")}</div>
        <div className="flex gap-1.5">
          <input
            data-testid="cu-interpreter-input"
            value={interpreter}
            spellCheck={false}
            onChange={(event) => setInterpreter(event.target.value)}
            placeholder={t("settingsExt.cu.interpreterPlaceholder")}
            className="border-chat-hairline bg-card text-chat-ink focus-visible:border-foreground h-7 min-w-0 flex-1 rounded-md border px-2 font-mono text-[11px] outline-none"
          />
          <Button
            size="sm"
            variant="outline"
            data-testid="cu-choose"
            onClick={() => setPickerOpen((prev) => !prev)}
          >
            {t("settingsExt.cu.choose")}
          </Button>
          <Button
            size="sm"
            data-testid="cu-interpreter-apply"
            disabled={busy || !interpreter.trim()}
            onClick={() => void applyInterpreter()}
          >
            {t("settingsExt.cu.apply")}
          </Button>
        </div>
        {pickerOpen ? (
          <div data-testid="cu-candidates" className="border-chat-hairline rounded-md border p-1">
            {(status?.interpreter.candidates ?? []).length === 0 ? (
              <p className="text-chat-muted px-1 py-1 text-[11px]">
                {t("settingsExt.cu.noCandidate")}
              </p>
            ) : (
              (status?.interpreter.candidates ?? []).map((candidate) => (
                <button
                  key={candidate.path}
                  type="button"
                  onClick={() => {
                    setInterpreter(candidate.path);
                    setPickerOpen(false);
                  }}
                  className="hover:bg-muted flex w-full items-center justify-between gap-2 rounded px-1.5 py-1 text-left font-mono text-[10px]"
                >
                  <span className="truncate">{candidate.path}</span>
                  <span className="text-chat-muted shrink-0">{candidate.version || "—"}</span>
                </button>
              ))
            )}
          </div>
        ) : null}
        <p className="text-chat-muted text-[10px] leading-4">{t("settingsExt.cu.interpreterNote")}</p>
      </div>

      <div className="text-chat-muted space-y-1 text-[11px]">
        <div className="flex items-center justify-between">
          <span>{t("settingsExt.cu.safeMode")}</span>
          <span className={status?.safe_mode === false ? "text-[#E24B4A]" : ""}>
            {status?.safe_mode === false
              ? t("settingsExt.cu.safeModeOff")
              : t("settingsExt.cu.safeModeOn")}
          </span>
        </div>
      </div>

      <p className="rounded-lg border border-[#FAC775] bg-[#FAEEDA] px-2.5 py-2 text-[11px] leading-5 text-[#854F0B]">
        {t("settingsExt.cu.notice")}
      </p>
    </div>
  );
}

export function AboutPanel() {
  const t = useT();
  const [info, setInfo] = useState<SystemInfo | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setInfo(await settingsApi.systemInfo());
      } catch {
        setInfo(null);
      }
    })();
  }, []);

  const rows: [string, string][] = [
    [t("settingsExt.about.version"), info?.version ?? "—"],
    [t("settingsExt.about.python"), info?.python ?? "—"],
    [t("settingsExt.about.uvicorn"), info?.uvicorn ?? "—"],
    [t("settingsExt.about.platform"), info?.platform ?? "—"],
    [t("settingsExt.about.dataDir"), info?.data_dir ?? "—"],
    [t("settingsExt.about.uptime"), info ? `${Math.round(info.uptime_s / 60)} min` : "—"],
  ];

  return (
    <div data-testid="settings-panel-about" className="space-y-3">
      <div>
        <h3 className="text-chat-ink text-xs font-medium">{t("settingsExt.about.title")}</h3>
        <p className="text-chat-muted mt-0.5 text-[11px]">{t("settingsExt.about.subtitle")}</p>
      </div>
      <div className="border-chat-hairline rounded-lg border">
        {rows.map(([label, value], index) => (
          <div
            key={label}
            className={
              index === 0
                ? "flex items-center justify-between px-2.5 py-2 text-[11px]"
                : "border-chat-hairline flex items-center justify-between border-t px-2.5 py-2 text-[11px]"
            }
          >
            <span>{label}</span>
            <span className="text-chat-muted font-mono">{value}</span>
          </div>
        ))}
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={() => {
          void settingsApi
            .systemInfo()
            .then(() => toast.success(t("settingsExt.about.refreshed")))
            .catch((error: unknown) => toast.error(errorText(error)));
        }}
      >
        <RefreshCw className="size-3" /> {t("settingsExt.about.refresh")}
      </Button>
    </div>
  );
}

function Switch({
  checked,
  onChange,
  testId,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void | Promise<void>;
  testId: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      data-testid={testId}
      aria-checked={checked}
      aria-disabled={disabled}
      disabled={disabled}
      onClick={() => void onChange(!checked)}
      className={
        checked
          ? "bg-foreground relative h-4 w-7 shrink-0 rounded-full transition-colors"
          : "bg-muted relative h-4 w-7 shrink-0 rounded-full transition-colors"
      }
    >
      <span
        className={
          checked
            ? "absolute top-0.5 left-3.5 size-3 rounded-full bg-white transition-transform"
            : "absolute top-0.5 left-0.5 size-3 rounded-full bg-white transition-transform"
        }
      />
    </button>
  );
}
