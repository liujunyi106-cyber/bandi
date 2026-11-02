/**
 * 功能：员工档案「人设」Tab（姓名/描述/状态/模型编辑 + 岗位与人设提示词只读）。
 */
import { useEffect, useState } from "react";

import { AvatarEditor } from "@/components/team/AvatarEditor";
import { Button } from "@/components/ui/button";
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
import { avatarMeta, type AvatarShapeId, type EmployeeAvatarMeta } from "@/lib/employee-avatar";
import type { EmployeeSummary } from "@/services/company";
import type { ModelOption } from "@/services/settings";

const GLOBAL_MODEL = "__global__";
const DEFAULT_THINKING = "__default__";

export interface EmployeePersonaTabProps {
  employee: EmployeeSummary;
  roleName: string;
  models: ModelOption[];
  globalModel: string;
  onSave: (patch: {
    name: string;
    description: string;
    avatar: EmployeeAvatarMeta;
    llm_config: { default_model?: string; thinking_mode?: string } | null;
  }) => Promise<void>;
}

/** 功能：人设表单与只读档案信息。 */
export function EmployeePersonaTab({
  employee,
  roleName,
  models,
  globalModel,
  onSave,
}: EmployeePersonaTabProps) {
  const t = useT();
  const personaValue = String(employee.metadata?.persona_prompt ?? employee.description ?? "");
  const initialAvatar = avatarMeta(employee.metadata);
  const [name, setName] = useState(employee.name);
  const [description, setDescription] = useState(personaValue);
  const [tone, setTone] = useState(initialAvatar?.tone ?? 0);
  const [shape, setShape] = useState<AvatarShapeId>(initialAvatar?.shape ?? "classic");
  const [avatarUrl, setAvatarUrl] = useState(
    initialAvatar?.kind === "upload" ? initialAvatar.url : "",
  );
  const [model, setModel] = useState(employee.llm_config?.default_model ?? GLOBAL_MODEL);
  const [thinking, setThinking] = useState(employee.llm_config?.thinking_mode ?? "");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const nextAvatar = avatarMeta(employee.metadata);
    setName(employee.name);
    setDescription(personaValue);
    setTone(nextAvatar?.tone ?? 0);
    setShape(nextAvatar?.shape ?? "classic");
    setAvatarUrl(nextAvatar?.kind === "upload" ? nextAvatar.url : "");
    setModel(employee.llm_config?.default_model ?? GLOBAL_MODEL);
    setThinking(employee.llm_config?.thinking_mode ?? "");
  }, [employee, personaValue]);

  const selectedModel = models.find((item) => item.name === model);
  const thinkingOptions = selectedModel?.supports_reasoning
    ? (selectedModel.reasoning_efforts ?? []).filter((item) => item && item !== "none")
    : [];

  const initialTone = initialAvatar?.tone ?? 0;
  const initialShape = initialAvatar?.shape ?? "classic";
  const initialAvatarUrl = initialAvatar?.kind === "upload" ? initialAvatar.url : "";
  const avatarDirty =
    tone !== initialTone || shape !== initialShape || avatarUrl !== initialAvatarUrl;
  const dirty =
    name.trim() !== employee.name ||
    description.trim() !== personaValue ||
    avatarDirty ||
    model !== (employee.llm_config?.default_model ?? GLOBAL_MODEL) ||
    thinking !== (employee.llm_config?.thinking_mode ?? "");

  const save = async () => {
    if (!name.trim()) return;
    setSaving(true);
    try {
      await onSave({
        name: name.trim(),
        description: description.trim(),
        avatar: avatarUrl
          ? { kind: "upload", tone, shape, url: avatarUrl }
          : { kind: "tone", tone, shape, url: "" },
        llm_config:
          model === GLOBAL_MODEL
            ? null
            : { default_model: model, ...(thinking ? { thinking_mode: thinking } : {}) },
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <section className="bg-card border-chat-hairline rounded-xl border p-4">
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
      </section>

      <section className="bg-card border-chat-hairline rounded-xl border p-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label={t("team.persona.name")}>
            <Input value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label={t("team.persona.role")}>
            <Input value={roleName || employee.role_id} readOnly disabled />
          </Field>
          <Field label={t("team.persona.model")}>
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
          <Field label={t("team.persona.thinking")}>
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
                  {t("team.persona.thinkingDefault")}
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
        <Field label={t("team.persona.persona")} className="mt-4">
          <Textarea
            rows={5}
            value={description}
            placeholder={t("team.persona.descriptionPlaceholder")}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <p className="text-chat-muted mt-2 text-[11px]">{t("team.persona.personaHint")}</p>
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button
            variant="outline"
            disabled={!dirty || saving}
            onClick={() => {
              const nextAvatar = avatarMeta(employee.metadata);
              setName(employee.name);
              setDescription(personaValue);
              setTone(nextAvatar?.tone ?? 0);
              setShape(nextAvatar?.shape ?? "classic");
              setAvatarUrl(nextAvatar?.kind === "upload" ? nextAvatar.url : "");
              setModel(employee.llm_config?.default_model ?? GLOBAL_MODEL);
              setThinking(employee.llm_config?.thinking_mode ?? "");
            }}
          >
            {t("team.persona.reset")}
          </Button>
          <Button disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? t("team.persona.saving") : t("team.persona.save")}
          </Button>
        </div>
      </section>
    </div>
  );
}

function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={`flex flex-col gap-1.5 ${className ?? ""}`}>
      <span className="text-chat-muted text-[11px] font-medium">{label}</span>
      {children}
    </label>
  );
}
