/**
 * 功能：创建员工三步向导（人 / 能力 / 组织）。
 * 员工名即岗位名，提交时创建同名岗位并绑定员工；组织归属与能力绑定均为可选项。
 */
import { Check, Plus, Sparkles, Upload, UserPlus } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { AvatarEditor } from "@/components/team/AvatarEditor";
import { ResourcePickerDialog } from "@/components/team/ResourcePickerDialog";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useT } from "@/i18n";
import { type AvatarShapeId } from "@/lib/employee-avatar";
import { cn } from "@/lib/utils";
import {
  describeError,
  teamApi,
  type KnowledgeBaseOption,
  type SopCardOption,
  type TeamOverview,
} from "@/services/team";

const GLOBAL_MODEL = "__global__";
const DEFAULT_THINKING = "__default__";
const NO_TEAM = "__none__";
const NO_MANAGER = "__none__";

type Step = 1 | 2 | 3;

export interface AddEmployeeDialogProps {
  open: boolean;
  overview: TeamOverview | null;
  onOpenChange: (open: boolean) => void;
  onCreated: () => void;
}

/** 功能：三步创建员工向导。 */
export function AddEmployeeDialog({
  open,
  overview,
  onOpenChange,
  onCreated,
}: AddEmployeeDialogProps) {
  const t = useT();
  const [step, setStep] = useState<Step>(1);
  const [employeeId, setEmployeeId] = useState("");
  const [name, setName] = useState("");
  const [persona, setPersona] = useState("");
  const [tone, setTone] = useState(0);
  const [shape, setShape] = useState<AvatarShapeId>("classic");
  const [avatarUrl, setAvatarUrl] = useState("");
  const [model, setModel] = useState(GLOBAL_MODEL);
  const [thinking, setThinking] = useState("");
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [sopIds, setSopIds] = useState<string[]>([]);
  const [knowledgeIds, setKnowledgeIds] = useState<string[]>([]);
  const [sopCards, setSopCards] = useState<SopCardOption[]>([]);
  const [knowledgeOptions, setKnowledgeOptions] = useState<KnowledgeBaseOption[]>([]);
  const [teamId, setTeamId] = useState(NO_TEAM);
  const [managerId, setManagerId] = useState(NO_MANAGER);
  const [subordinateIds, setSubordinateIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [pickerKind, setPickerKind] = useState<"skill" | "general_skill" | "knowledge_base" | null>(
    null,
  );
  const [pickerSelected, setPickerSelected] = useState<string[]>([]);
  const resourceFileRef = useRef<HTMLInputElement>(null);

  const roles = useMemo(() => {
    const orgRoleIds = new Set(
      (overview?.orgTeams ?? []).flatMap((team) => team.roles.map((role) => role.role_id)),
    );
    const memberRoleIds = new Set((overview?.employees ?? []).map((employee) => employee.role_id));
    return (overview?.roles ?? []).filter(
      (role) => orgRoleIds.has(role.id) || memberRoleIds.has(role.id),
    );
  }, [overview]);
  const models = overview?.models?.available_models ?? [];
  const globalModel = overview?.models?.effective_model ?? "";
  const selectedModel = models.find((item) => item.name === model);
  const thinkingOptions = selectedModel?.supports_reasoning
    ? (selectedModel.reasoning_efforts ?? []).filter((item) => item && item !== "none")
    : [];
  const teams = overview?.orgTeams ?? [];
  const skills = useMemo(() => overview?.skills ?? [], [overview]);

  useEffect(() => {
    if (!open) return;
    setStep(1);
    setEmployeeId(`emp-${Date.now().toString(36)}`);
    setName("");
    setPersona("");
    setTone(0);
    setShape("classic");
    setAvatarUrl("");
    setModel(GLOBAL_MODEL);
    setThinking("");
    setSkillIds([]);
    setSopIds([]);
    setKnowledgeIds([]);
    setPickerKind(null);
    setPickerSelected([]);
    setTeamId(NO_TEAM);
    setManagerId(NO_MANAGER);
    setSubordinateIds([]);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    void Promise.allSettled([teamApi.skillCards(), teamApi.knowledgeBases()]).then(
      ([cards, bases]) => {
        setSopCards(cards.status === "fulfilled" ? cards.value.cards : []);
        setKnowledgeOptions(bases.status === "fulfilled" ? bases.value.bases : []);
      },
    );
  }, [open]);

  const pickerItems = useMemo(() => {
    if (pickerKind === "general_skill") {
      return skills.map((skill) => ({
        id: skill.name,
        label: skill.name,
        hint: skill.description,
      }));
    }
    if (pickerKind === "skill") {
      return sopCards.map((card) => ({
        id: card.skill_id,
        label: card.name,
        hint: card.description,
      }));
    }
    if (pickerKind === "knowledge_base") {
      return knowledgeOptions.map((base) => ({
        id: base.base_id,
        label: base.name,
        hint: `${base.document_count} ${t("team.resources.documents")}`,
      }));
    }
    return [];
  }, [knowledgeOptions, pickerKind, skills, sopCards, t]);

  const applyPicker = () => {
    if (pickerKind === "general_skill") {
      setSkillIds((current) => Array.from(new Set([...current, ...pickerSelected])));
    } else if (pickerKind === "skill") {
      setSopIds((current) => Array.from(new Set([...current, ...pickerSelected])));
    } else if (pickerKind === "knowledge_base") {
      setKnowledgeIds((current) => Array.from(new Set([...current, ...pickerSelected])));
    }
    setPickerKind(null);
    setPickerSelected([]);
  };

  const selectedCountFor = (kind: "skill" | "general_skill" | "knowledge_base") =>
    kind === "skill"
      ? sopIds.length
      : kind === "general_skill"
        ? skillIds.length
        : knowledgeIds.length;

  const availableCountFor = (kind: "skill" | "general_skill" | "knowledge_base") =>
    kind === "skill"
      ? sopCards.length
      : kind === "general_skill"
        ? skills.length
        : knowledgeOptions.length;

  const uploadResource = async (file: File) => {
    if (!pickerKind) return;
    try {
      const kind =
        pickerKind === "skill" ? "sop" : pickerKind === "general_skill" ? "skill" : "knowledge";
      await teamApi.importResource(kind, file, {
        baseId: pickerKind === "knowledge_base" ? "shared" : undefined,
      });
      toast.success(t("team.resources.uploaded"));
      const [cards, bases] = await Promise.allSettled([
        teamApi.skillCards(),
        teamApi.knowledgeBases(),
      ]);
      setSopCards(cards.status === "fulfilled" ? cards.value.cards : []);
      setKnowledgeOptions(bases.status === "fulfilled" ? bases.value.bases : []);
    } catch (error) {
      toast.error(describeError(error, t("team.resources.uploadFailed")));
    }
  };

  const toggleSubordinate = (id: string) =>
    setSubordinateIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );

  const submit = async () => {
    if (!name.trim()) return;
    setSaving(true);
    try {
      const hasOrg = teamId !== NO_TEAM || managerId !== NO_MANAGER || subordinateIds.length > 0;
      const roleId = hasOrg ? employeeId : "";
      if (hasOrg) {
        await teamApi.createRole({
          id: roleId,
          name: name.trim(),
          responsibility: persona.trim(),
          reports_to: managerId === NO_MANAGER ? null : managerId,
          can_spawn: subordinateIds,
          team_id: teamId === NO_TEAM ? "" : teamId,
        });
      }
      await teamApi.createEmployee({
        employee_id: employeeId,
        name: name.trim(),
        role_id: roleId,
        description: persona.trim(),
        llm_config:
          model === GLOBAL_MODEL
            ? null
            : { default_model: model, ...(thinking ? { thinking_mode: thinking } : {}) },
        metadata: {
          persona_prompt: persona.trim(),
          avatar: avatarUrl ? { kind: "upload", url: avatarUrl } : { kind: "tone", tone, shape },
        },
      });
      let bindFailed = 0;
      const bindings: Array<[string, string]> = [
        ...skillIds.map((id) => ["general_skill", id] as [string, string]),
        ...sopIds.map((id) => ["skill", id] as [string, string]),
        ...knowledgeIds.map((id) => ["knowledge_base", id] as [string, string]),
      ];
      for (const [resourceType, resourceId] of bindings) {
        try {
          await teamApi.bindResource(employeeId, resourceType, resourceId);
        } catch {
          bindFailed += 1;
        }
      }
      toast.success(t("team.add.created"));
      if (bindFailed > 0) toast.info(`${t("team.add.bindPending")} ${bindFailed}`);
      onCreated();
      onOpenChange(false);
    } catch (error) {
      toast.error(describeError(error, t("team.add.createFailed")));
    } finally {
      setSaving(false);
    }
  };

  const canNext = step === 1 ? name.trim().length > 0 : true;

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="flex max-h-[calc(100dvh-4rem)] flex-col gap-4 overflow-hidden sm:max-w-[640px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-sm">
              <UserPlus className="size-4" />
              {t("team.add.title")}
            </DialogTitle>
            <DialogDescription className="text-chat-muted text-[11px]">
              {t("team.add.description")}
            </DialogDescription>
          </DialogHeader>

          <div className="flex items-center gap-2">
            {[1, 2, 3].map((item) => (
              <button
                key={item}
                type="button"
                onClick={() => item < step && setStep(item as Step)}
                className={cn(
                  "flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] transition-colors",
                  step === item
                    ? "bg-primary text-primary-foreground"
                    : item < step
                      ? "bg-primary/5 text-chat-ink"
                      : "text-chat-muted",
                )}
              >
                <span className="grid size-4 place-items-center rounded-full border border-current text-[9px]">
                  {item < step ? <Check className="size-2.5" /> : item}
                </span>
                {t(`team.add.step${item}`)}
              </button>
            ))}
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            {step === 1 ? (
              <div className="flex flex-col gap-4">
                <AvatarEditor
                  name={name}
                  tone={tone}
                  shape={shape}
                  avatarUrl={avatarUrl}
                  onChange={(value) => {
                    setTone(value.tone);
                    setShape(value.shape);
                    setAvatarUrl(value.avatarUrl);
                  }}
                />

                <Field label={t("team.add.name")}>
                  <Input
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    placeholder={t("team.add.namePlaceholder")}
                  />
                </Field>
                <Field label={t("team.add.persona")}>
                  <Textarea
                    rows={5}
                    value={persona}
                    onChange={(event) => setPersona(event.target.value)}
                    placeholder={t("team.add.personaPlaceholder")}
                  />
                </Field>
                <p className="text-chat-muted text-[11px]">{t("team.add.personaHint")}</p>
              </div>
            ) : step === 2 ? (
              <div className="flex flex-col gap-4">
                {[
                  {
                    kind: "skill" as const,
                    label: t("team.resources.sop"),
                    hint: t("team.add.noSop"),
                  },
                  {
                    kind: "general_skill" as const,
                    label: t("team.resources.skills"),
                    hint: t("team.add.noSkills"),
                  },
                  {
                    kind: "knowledge_base" as const,
                    label: t("team.resources.knowledge"),
                    hint: t("team.add.noKnowledge"),
                  },
                ].map((group) => (
                  <section key={group.kind}>
                    <div className="flex items-center justify-between">
                      <h3 className="text-chat-ink text-xs font-semibold">{group.label}</h3>
                      {selectedCountFor(group.kind) > 0 ? (
                        <span className="text-chat-muted text-[10px]">
                          {t("team.resources.selected")} {selectedCountFor(group.kind)}
                        </span>
                      ) : null}
                    </div>
                    <button
                      type="button"
                      onClick={() => {
                        setPickerKind(group.kind);
                        setPickerSelected([]);
                      }}
                      className="border-chat-hairline text-chat-muted hover:text-chat-ink mt-2 flex w-full items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-left text-xs transition-colors"
                    >
                      <Plus className="size-3.5 shrink-0" />
                      <span className="flex-1">{t("team.resources.chooseInstalled")}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        resourceFileRef.current?.click();
                      }}
                      className="border-chat-hairline text-chat-muted hover:text-chat-ink mt-1.5 flex w-full items-center gap-2 rounded-lg border border-dashed px-3 py-2 text-left text-xs transition-colors"
                    >
                      <Upload className="size-3.5 shrink-0" />
                      <span className="flex-1">{t("team.resources.upload")}</span>
                    </button>
                    <p className="text-chat-muted mt-1 text-[10px]">
                      {group.kind === "general_skill"
                        ? `${t("team.add.sharedSkills")} ${skills.length}`
                        : `${t("team.resources.available")} ${availableCountFor(group.kind)}`}
                    </p>
                  </section>
                ))}
                <Field label={t("team.add.model")}>
                  <Select
                    value={model}
                    onValueChange={(value) => {
                      setModel(value);
                      setThinking("");
                    }}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={GLOBAL_MODEL}>
                        {t("team.persona.followGlobal")}
                        {globalModel ? ` · ${globalModel}` : ""}
                      </SelectItem>
                      {models.map((item) => (
                        <SelectItem key={item.name} value={item.name}>
                          {item.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Field label={t("team.add.thinking")}>
                  <Select
                    value={thinking || DEFAULT_THINKING}
                    onValueChange={(value) => setThinking(value === DEFAULT_THINKING ? "" : value)}
                    disabled={model === GLOBAL_MODEL || thinkingOptions.length === 0}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={DEFAULT_THINKING}>
                        {t("team.add.thinkingDefault")}
                      </SelectItem>
                      {thinkingOptions.map((effort) => (
                        <SelectItem key={effort} value={effort}>
                          {effort}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
              </div>
            ) : (
              <div className="flex flex-col gap-4">
                <p className="text-chat-muted text-[11px]">{t("team.add.orgHint")}</p>
                <Field label={t("team.add.team")}>
                  <Select value={teamId} onValueChange={setTeamId}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_TEAM}>{t("team.add.noTeam")}</SelectItem>
                      {teams.map((team) => (
                        <SelectItem key={team.team_id} value={team.team_id}>
                          {team.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Field label={t("team.add.manager")}>
                  <Select value={managerId} onValueChange={setManagerId}>
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NO_MANAGER}>{t("team.add.noManager")}</SelectItem>
                      {roles.map((role) => (
                        <SelectItem key={role.id} value={role.id}>
                          {role.name} · {role.id}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <div>
                  <p className="text-chat-muted text-[11px] font-medium">
                    {t("team.add.subordinates")}
                  </p>
                  {roles.length === 0 ? (
                    <p className="text-chat-muted mt-2 text-[11px]">{t("team.add.noRoles")}</p>
                  ) : (
                    <div className="border-chat-hairline mt-2 max-h-40 overflow-y-auto rounded-lg border p-1">
                      {roles.map((role) => {
                        const active = subordinateIds.includes(role.id);
                        return (
                          <button
                            key={role.id}
                            type="button"
                            onClick={() => toggleSubordinate(role.id)}
                            className="hover:bg-primary/5 flex w-full items-center gap-2 rounded px-2 py-1.5 text-left"
                          >
                            <span
                              className={cn(
                                "grid size-4 shrink-0 place-items-center rounded border",
                                active
                                  ? "border-primary bg-primary text-primary-foreground"
                                  : "border-chat-hairline",
                              )}
                            >
                              {active ? <Check className="size-3" /> : null}
                            </span>
                            <span className="text-chat-ink text-[11px]">{role.name}</span>
                            <span className="text-chat-muted ml-auto text-[10px]">{role.id}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
                <p className="text-chat-muted text-[11px]">
                  <Sparkles className="mr-1 inline size-3" />
                  {t("team.add.mergeHint")}
                </p>
              </div>
            )}
          </div>

          <DialogFooter className="flex items-center justify-between gap-2 sm:justify-between">
            <Button
              variant="ghost"
              onClick={() => (step === 1 ? onOpenChange(false) : setStep((step - 1) as Step))}
            >
              {step === 1 ? t("team.add.cancel") : t("team.add.back")}
            </Button>
            {step < 3 ? (
              <Button disabled={!canNext} onClick={() => setStep((step + 1) as Step)}>
                {t("team.add.next")}
              </Button>
            ) : (
              <Button disabled={saving || !name.trim()} onClick={() => void submit()}>
                {saving ? t("team.add.creating") : t("team.add.create")}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ResourcePickerDialog
        open={pickerKind !== null}
        title={`${t("team.resources.pickTitle")} · ${
          pickerKind === "general_skill"
            ? t("team.resources.skills")
            : pickerKind === "skill"
              ? t("team.resources.sop")
              : t("team.resources.knowledge")
        }`}
        items={pickerItems}
        selectedIds={pickerSelected}
        onOpenChange={(next) => !next && setPickerKind(null)}
        onToggle={(id) =>
          setPickerSelected((current) =>
            current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
          )
        }
        onSubmit={applyPicker}
        onUpload={(file) => void uploadResource(file)}
      />

      <input
        ref={resourceFileRef}
        type="file"
        className="hidden"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void uploadResource(file);
          event.target.value = "";
        }}
      />
    </>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-chat-muted text-[11px] font-medium">{label}</span>
      {children}
    </label>
  );
}
