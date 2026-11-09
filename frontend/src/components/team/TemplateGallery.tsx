/**
 * 功能：模板 Tab（团队模板 / 人才模板二级切换 + 应用确认）。
 * 团队模板应用 = 实例化整队（角色 + 员工 + 席位 + 档案）；人才模板应用 = 招募单个员工。
 */
import { ChevronDown } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { OrgTeamSummary, TeamTemplateSummary } from "@/services/company";
import { describeError, teamApi, type TalentTemplateSummary } from "@/services/team";

type TemplateTab = "team" | "talent";

/** 功能：团队 ID 口径——与后端 `[A-Za-z0-9._-]{1,64}` 一致（决定员工 id 前缀）。 */
const TEAM_ID_MAX = 64;
const TEAM_ID_PATTERN = /^[A-Za-z0-9._-]+$/;

/** 功能：默认团队 ID——与已有团队重名时自动加序号，避免一点确认就失败。 */
function suggestTeamId(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  let index = 2;
  while (taken.has(`${base}-${index}`)) index += 1;
  return `${base}-${index}`;
}

export interface TemplateGalleryProps {
  templates: TeamTemplateSummary[];
  orgTeams: OrgTeamSummary[];
  /** 已占用岗位 id——人才模板自动派生岗位时避重。 */
  roleIds?: string[];
  onApplied: () => void;
}

/** 功能：模板画廊（团队模板网格 / 人才模板网格）。 */
export function TemplateGallery({
  templates,
  orgTeams,
  roleIds = [],
  onApplied,
}: TemplateGalleryProps) {
  const t = useT();
  const [tab, setTab] = useState<TemplateTab>("team");
  const [talents, setTalents] = useState<TalentTemplateSummary[]>([]);
  const [talentError, setTalentError] = useState("");
  const [teamTarget, setTeamTarget] = useState<TeamTemplateSummary | null>(null);
  const [talentTarget, setTalentTarget] = useState<TalentTemplateSummary | null>(null);
  const [failReason, setFailReason] = useState("");

  useEffect(() => {
    let cancelled = false;
    teamApi
      .talentTemplates()
      .then((data) => {
        if (!cancelled) setTalents(data.templates ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setTalentError(describeError(error, t("team.templates.loadFailed")));
      });
    return () => {
      cancelled = true;
    };
  }, [t]);

  const talentCategories = useMemo(() => {
    const counts = new Map<string, number>();
    talents.forEach((item) => counts.set(item.category, (counts.get(item.category) ?? 0) + 1));
    return counts;
  }, [talents]);

  return (
    <div className="flex min-h-[600px] flex-col gap-5">
      <div className="border-chat-hairline flex items-end gap-4 border-b">
        {(
          [
            { value: "team" as TemplateTab, label: t("team.templates.teamGroup"), count: templates.length },
            { value: "talent" as TemplateTab, label: t("team.templates.talentGroup"), count: talents.length },
          ] as const
        ).map((item, index) => (
          <div key={item.value} className="flex items-end gap-4">
            {index > 0 ? <span className="bg-chat-hairline mb-2.5 h-3 w-px" aria-hidden="true" /> : null}
            <button
              type="button"
              data-testid={`template-tab-${item.value}`}
              onClick={() => setTab(item.value)}
              className={cn(
                "flex items-baseline gap-1.5 border-b-2 pb-2.5 pt-0.5 text-xs transition-colors",
                tab === item.value
                  ? "border-primary text-chat-ink font-semibold"
                  : "text-chat-muted hover:text-chat-ink border-transparent",
              )}
            >
              {item.label}
              <span className="text-chat-muted/80 text-[10px] font-normal">{item.count}</span>
            </button>
          </div>
        ))}
        <span className="text-chat-muted/80 mb-2.5 ml-auto text-[11px]">
          {tab === "team" ? t("team.templates.teamHint") : t("team.templates.talentHint")}
        </span>
      </div>

      {tab === "team" ? (
        templates.length === 0 ? (
          <p className="text-chat-muted px-6 py-12 text-center text-xs">
            {t("team.templates.empty")}
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {templates.map((template) => {
              const roleCount = template.role_count ?? template.roles?.length ?? 0;
              const roleNames = (template.roles ?? []).map((role) => role.name);
              return (
                <article
                  key={template.template_id}
                  data-testid={`team-template-${template.template_id}`}
                  className="bg-card border-chat-hairline flex flex-col gap-3 rounded-2xl border p-4"
                >
                  <div className="flex items-start justify-between gap-2">
                    <strong className="text-chat-ink text-[13px] font-semibold">
                      {template.name}
                    </strong>
                    <span className="text-chat-muted shrink-0 rounded-full border border-chat-hairline px-2 py-0.5 text-[10px]">
                      {template.created_at ? t("team.templates.custom") : t("team.templates.builtin")}
                    </span>
                  </div>
                  <p className="text-chat-muted line-clamp-2 min-h-9 text-[11.5px] leading-relaxed">
                    {template.description || t("team.templates.noDescription")}
                  </p>
                  <div className="text-chat-muted flex items-center gap-3 text-[11px]">
                    <span>
                      <b className="text-chat-ink font-semibold">{roleCount}</b>{" "}
                      {t("team.templates.roles")}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {roleNames.slice(0, 4).map((name) => (
                      <span
                        key={name}
                        className="border-chat-hairline text-chat-muted rounded-md border px-1.5 py-0.5 text-[10px]"
                      >
                        {name}
                      </span>
                    ))}
                    {roleNames.length > 4 ? (
                      <span className="text-chat-muted/80 px-1 py-0.5 text-[10px]">
                        +{roleNames.length - 4}
                      </span>
                    ) : null}
                  </div>
                  <div className="border-chat-hairline mt-auto flex items-center justify-between border-t pt-2.5">
                    <span className="text-chat-muted/70 text-[10px]">
                      {t("team.templates.teamCardHint")}
                    </span>
                    <Button size="sm" onClick={() => setTeamTarget(template)}>
                      {t("team.templates.apply")}
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        )
      ) : talentError ? (
        <p className="text-chat-muted px-6 py-12 text-center text-xs">{talentError}</p>
      ) : talents.length === 0 ? (
        <p className="text-chat-muted px-6 py-12 text-center text-xs">
          {t("team.templates.empty")}
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {talents.map((talent) => (
            <article
              key={talent.name}
              data-testid={`talent-template-${talent.name}`}
              className="bg-card border-chat-hairline flex flex-col gap-3 rounded-2xl border p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <strong className="text-chat-ink text-[13px] font-semibold">
                  {talent.role_id || talent.name}
                </strong>
                <span className="text-chat-muted shrink-0 rounded-full border border-chat-hairline px-2 py-0.5 text-[10px]">
                  {talentCategories.get(talent.category) ? talent.category : talent.category}
                </span>
              </div>
              <p className="text-chat-muted line-clamp-2 min-h-9 text-[11.5px] leading-relaxed">
                {talent.description || t("team.templates.noDescription")}
              </p>
              <div className="text-chat-muted flex items-center gap-3 text-[11px]">
                <span>
                  {t("team.templates.templateId")} · {talent.name}
                </span>
              </div>
              <div className="border-chat-hairline mt-auto flex items-center justify-between border-t pt-2.5">
                <span className="text-chat-muted/70 text-[10px]">
                  {t("team.templates.talentCardHint")}
                </span>
                <Button size="sm" variant="outline" onClick={() => setTalentTarget(talent)}>
                  {t("team.templates.apply")}
                </Button>
              </div>
            </article>
          ))}
        </div>
      )}

      <TeamApplyDialog
        template={teamTarget}
        orgTeams={orgTeams}
        onOpenChange={(open) => {
          if (!open) setTeamTarget(null);
        }}
        onFail={(reason) => setFailReason(reason)}
        onApplied={() => {
          setTeamTarget(null);
          onApplied();
        }}
      />

      <TalentApplyDialog
        template={talentTarget}
        orgTeams={orgTeams}
        roleIds={roleIds}
        onOpenChange={(open) => {
          if (!open) setTalentTarget(null);
        }}
        onApplied={() => {
          setTalentTarget(null);
          onApplied();
        }}
      />

      <AlertDialog
        open={Boolean(failReason)}
        onOpenChange={(open) => {
          if (!open) setFailReason("");
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("team.templates.failTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{failReason}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setFailReason("")}>
              {t("team.templates.failOk")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

interface TeamApplyDialogProps {
  template: TeamTemplateSummary | null;
  orgTeams: OrgTeamSummary[];
  onOpenChange: (open: boolean) => void;
  onApplied: () => void;
  onFail: (reason: string) => void;
}

/** 功能：团队模板应用确认（填团队名称，团队 id 自动生成可展开手改）。 */
function TeamApplyDialog({
  template,
  orgTeams,
  onOpenChange,
  onApplied,
  onFail,
}: TeamApplyDialogProps) {
  const t = useT();
  const [teamName, setTeamName] = useState("");
  const [teamId, setTeamId] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);

  const takenIds = useMemo(() => new Set(orgTeams.map((team) => team.team_id)), [orgTeams]);

  useEffect(() => {
    if (!template) return;
    setTeamName(template.name);
    setTeamId(suggestTeamId(template.template_id, takenIds));
    setAdvanced(false);
    // 仅随模板切换重置；orgTeams 变化不覆盖用户输入
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [template]);

  const trimmedName = teamName.trim();
  const trimmedId = teamId.trim();
  const nameError = trimmedName ? "" : t("team.templates.teamNameRequired");
  const idError = !trimmedId
    ? t("team.templates.teamIdRequired")
    : !TEAM_ID_PATTERN.test(trimmedId)
      ? t("team.templates.teamIdInvalid")
      : trimmedId.length > TEAM_ID_MAX
        ? t("team.templates.teamIdTooLong").replace("{max}", String(TEAM_ID_MAX))
        : takenIds.has(trimmedId)
          ? t("team.templates.teamIdExists")
          : "";
  const roles = template?.roles ?? [];
  const roleNameById = useMemo(
    () => new Map((template?.roles ?? []).map((role) => [role.id, role.name])),
    [template],
  );

  const onNameChange = (value: string) => setTeamName(value);

  const submit = async () => {
    if (!template || idError || nameError) return;
    setBusy(true);
    try {
      const result = await teamApi.instantiateTeam(template.template_id, trimmedId, trimmedName);
      const created = result.report?.created_employee_ids?.length ?? 0;
      toast.success(t("team.templates.created").replace("{count}", String(created)));
      onApplied();
    } catch (error) {
      onOpenChange(false);
      onFail(describeError(error, t("team.templates.failGeneric")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={template !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle className="text-sm">
            {t("team.templates.applyTitle").replace("{name}", template?.name ?? "")}
          </DialogTitle>
          <DialogDescription className="text-chat-muted text-[11px]">
            {t("team.templates.applyDesc").replace(
              "{roles}",
              String(template?.role_count ?? roles.length),
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-chat-muted text-[11px]">{t("team.templates.teamNameLabel")}</span>
            <Input
              value={teamName}
              onChange={(event) => onNameChange(event.target.value)}
              aria-invalid={Boolean(nameError)}
              className={cn("h-8 text-xs", nameError && "border-destructive")}
              data-testid="template-team-name"
            />
            {nameError ? (
              <span className="text-destructive text-[10px]">{nameError}</span>
            ) : null}
          </label>

          <div className="flex flex-col gap-1.5">
            <button
              type="button"
              onClick={() => setAdvanced((value) => !value)}
              className="text-chat-muted hover:text-chat-ink flex items-center gap-1 self-start text-[10.5px]"
              data-testid="template-team-id-toggle"
            >
              <ChevronDown className={cn("size-3 transition-transform", advanced && "rotate-180")} />
              {t("team.templates.teamIdAdvanced")}
            </button>
            {advanced ? (
              <>
                <Input
                  value={teamId}
                  onChange={(event) => setTeamId(event.target.value)}
                  maxLength={TEAM_ID_MAX}
                  aria-invalid={Boolean(idError)}
                  aria-describedby="template-team-id-hint"
                  className={cn(
                    "h-8 text-xs",
                    idError && "border-destructive focus-visible:ring-destructive",
                  )}
                  data-testid="template-team-id"
                />
                <span
                  id="template-team-id-hint"
                  className={cn(
                    "flex items-center justify-between gap-3 text-[10px]",
                    idError ? "text-destructive" : "text-chat-muted",
                  )}
                >
                  <span data-testid="template-team-id-hint">
                    {idError || t("team.templates.teamIdRules").replace("{max}", String(TEAM_ID_MAX))}
                  </span>
                  <span className="shrink-0 tabular-nums">
                    {trimmedId.length} / {TEAM_ID_MAX}
                  </span>
                </span>
              </>
            ) : (
              <span className="text-chat-muted/80 text-[10px]">
                {t("team.templates.teamIdAuto").replace("{id}", trimmedId)}
              </span>
            )}
          </div>

          <div className="border-chat-hairline max-h-56 overflow-y-auto rounded-xl border">
            {roles.map((role, index) => (
              <div
                key={role.id}
                className="border-chat-hairline flex items-center gap-2 border-b px-3 py-2 text-[11.5px] last:border-b-0"
              >
                <span className="text-chat-ink min-w-[72px] font-medium">{role.name}</span>
                <span className="text-chat-muted min-w-0 flex-1 truncate">
                  {role.responsibility || t("team.templates.noResponsibility")}
                </span>
                <span className="text-chat-muted/80 shrink-0 text-[10px]">
                  {index === 0 || !role.reports_to
                    ? t("team.templates.topLevel")
                    : t("team.templates.reportsTo").replace(
                        "{name}",
                        roleNameById.get(role.reports_to) ?? role.reports_to,
                      )}
                </span>
              </div>
            ))}
          </div>

          <div className="flex flex-col gap-1.5">
            <span className="text-chat-muted text-[11px]">{t("team.templates.taskGraphHint")}</span>
          </div>

          <p className="bg-primary/[0.02] border-chat-hairline text-chat-muted rounded-xl border px-3 py-2 text-[11px] leading-relaxed">
            {t("team.templates.note")
              .replace("{id}", trimmedName || template?.name || "")
              .replace("{roles}", String(template?.role_count ?? roles.length))}
          </p>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            disabled={busy || Boolean(idError) || Boolean(nameError)}
            onClick={() => void submit()}
            data-testid="template-apply-confirm"
          >
            {busy ? t("team.templates.applying") : t("team.templates.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface TalentApplyDialogProps {
  template: TalentTemplateSummary | null;
  orgTeams: OrgTeamSummary[];
  roleIds?: string[];
  onOpenChange: (open: boolean) => void;
  onApplied: () => void;
}

/** 功能：人才模板应用（只填姓名与目标团队，岗位自动派生）。 */
function TalentApplyDialog({
  template,
  orgTeams,
  roleIds = [],
  onOpenChange,
  onApplied,
}: TalentApplyDialogProps) {
  const t = useT();
  const [name, setName] = useState("");
  const [teamId, setTeamId] = useState("");
  const [busy, setBusy] = useState(false);

  const takenRoles = useMemo(() => new Set(roleIds), [roleIds]);

  useEffect(() => {
    if (!template) return;
    setName(template.role_id || template.name);
    setTeamId("");
  }, [template]);

  const roleId = useMemo(
    () => (template ? suggestTeamId(template.name, takenRoles) : ""),
    [template, takenRoles],
  );

  const submit = async () => {
    if (!template) return;
    setBusy(true);
    try {
      await teamApi.hireTalent({
        template_name: template.name,
        name: name.trim() || template.name,
        role_id: roleId,
        team_id: teamId,
      });
      const teamName = orgTeams.find((team) => team.team_id === teamId)?.name;
      toast.success(
        teamName
          ? t("team.templates.talentCreatedInTeam")
              .replace("{name}", name.trim() || template.name)
              .replace("{team}", teamName)
          : t("team.templates.talentCreated").replace("{name}", name.trim() || template.name),
      );
      onApplied();
    } catch (error) {
      toast.error(describeError(error, t("team.templates.failGeneric")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={template !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle className="text-sm">
            {t("team.templates.talentApplyTitle").replace(
              "{name}",
              template?.role_id || template?.name || "",
            )}
          </DialogTitle>
          <DialogDescription className="text-chat-muted text-[11px]">
            {template?.description || t("team.templates.talentApplyDesc")}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-chat-muted text-[11px]">{t("team.templates.talentName")}</span>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="h-8 text-xs"
              data-testid="talent-name"
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-chat-muted text-[11px]">{t("team.templates.talentTeam")}</span>
            <select
              value={teamId}
              onChange={(event) => setTeamId(event.target.value)}
              className="border-chat-hairline bg-card h-8 rounded-md border px-2 text-xs"
              data-testid="talent-team"
            >
              <option value="">{t("team.templates.talentNoTeam")}</option>
              {orgTeams.map((team) => (
                <option key={team.team_id} value={team.team_id}>
                  {team.name}
                </option>
              ))}
            </select>
          </label>
          <span className="text-chat-muted/80 text-[10px]" data-testid="talent-role-auto">
            {t("team.templates.talentRoleAuto").replace("{id}", roleId)}
          </span>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button disabled={busy} onClick={() => void submit()} data-testid="talent-apply-confirm">
            {busy ? t("team.templates.applying") : t("team.templates.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
