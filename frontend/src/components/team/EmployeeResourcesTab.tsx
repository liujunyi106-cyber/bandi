/**
 * 功能：员工档案「技能与资源」Tab（SOP / 技能 / 知识库 / 工具多选绑定 + 解绑 + 上传本地文件）。
 * 资源为共享资源，可绑定给多个员工。
 */
import { BookOpen, Plus, Sparkles, Wrench, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { ResourcePickerDialog, type ResourceOption } from "@/components/team/ResourcePickerDialog";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import {
  describeError,
  teamApi,
  type EmployeeKnowledgeBase,
  type EmployeeProfile,
  type EmployeeResource,
  type KnowledgeBaseOption,
  type SopCardOption,
  type TeamOverview,
} from "@/services/team";

type ResourceKind = "skill" | "general_skill" | "knowledge_base" | "tool";

interface ResourceGroup {
  kind: ResourceKind;
  labelKey: string;
  icon: typeof Sparkles;
}

const GROUPS: ResourceGroup[] = [
  { kind: "skill", labelKey: "team.resources.sop", icon: Sparkles },
  { kind: "general_skill", labelKey: "team.resources.skills", icon: Sparkles },
  { kind: "knowledge_base", labelKey: "team.resources.knowledge", icon: BookOpen },
  { kind: "tool", labelKey: "team.resources.tools", icon: Wrench },
];

export interface EmployeeResourcesTabProps {
  employeeId: string;
  profile: EmployeeProfile | null;
  knowledge: EmployeeKnowledgeBase[];
  overview: TeamOverview | null;
  onChanged: () => void;
}

/** 功能：四组资源绑定（多选）、解绑与上传。 */
export function EmployeeResourcesTab({
  employeeId,
  profile,
  knowledge,
  overview,
  onChanged,
}: EmployeeResourcesTabProps) {
  const t = useT();
  const [pickerKind, setPickerKind] = useState<ResourceKind | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [sopCards, setSopCards] = useState<SopCardOption[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseOption[]>([]);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([teamApi.skillCards(), teamApi.knowledgeBases(employeeId)]).then(
      ([cards, bases]) => {
        if (cancelled) return;
        setSopCards(cards.status === "fulfilled" ? cards.value.cards : []);
        setKnowledgeBases(bases.status === "fulfilled" ? bases.value.bases : []);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [employeeId]);

  const resources = useMemo(() => profile?.resources ?? [], [profile]);
  const boundByKind = useMemo(() => {
    const map = new Map<ResourceKind, EmployeeResource[]>();
    for (const group of GROUPS) {
      map.set(
        group.kind,
        resources.filter((item) => item.resource_type === group.kind),
      );
    }
    if (knowledge.length > 0 && (map.get("knowledge_base")?.length ?? 0) === 0) {
      map.set(
        "knowledge_base",
        knowledge.map((base) => ({
          resource_type: "knowledge_base",
          resource_id: base.base_id,
          status: "active",
          metadata: { name: base.name, document_count: base.document_count },
        })),
      );
    }
    return map;
  }, [knowledge, resources]);

  const candidates = useMemo<ResourceOption[]>(() => {
    if (pickerKind === "general_skill") {
      return (overview?.skills ?? []).map((skill) => ({
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
      return knowledgeBases.map((base) => ({
        id: base.base_id,
        label: base.name,
        hint: `${base.document_count} ${t("team.resources.documents")}`,
      }));
    }
    if (pickerKind === "tool") {
      return (overview?.tools ?? []).map((tool) => ({
        id: tool.name,
        label: tool.name,
        hint: tool.description ?? "",
      }));
    }
    return [];
  }, [knowledgeBases, overview?.skills, overview?.tools, pickerKind, sopCards, t]);

  const openPicker = (kind: ResourceKind) => {
    setSelectedIds([]);
    setPickerKind(kind);
  };

  const toggle = (id: string) =>
    setSelectedIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );

  const submit = async () => {
    if (!pickerKind || selectedIds.length === 0) return;
    setSubmitting(true);
    let failed = 0;
    for (const id of selectedIds) {
      try {
        await teamApi.bindResource(employeeId, pickerKind, id);
      } catch {
        failed += 1;
      }
    }
    setSubmitting(false);
    setPickerKind(null);
    setSelectedIds([]);
    if (failed === 0) toast.success(t("team.resources.boundDone"));
    else toast.error(`${t("team.resources.bindFailed")} ${failed}`);
    onChanged();
  };

  const unbind = async (kind: ResourceKind, id: string) => {
    try {
      await teamApi.unbindResource(employeeId, kind, id);
      toast.success(t("team.resources.unbound"));
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.resources.unbindFailed")));
    }
  };

  const reloadCandidates = () => {
    void Promise.allSettled([teamApi.skillCards(), teamApi.knowledgeBases(employeeId)]).then(
      ([cards, bases]) => {
        setSopCards(cards.status === "fulfilled" ? cards.value.cards : []);
        setKnowledgeBases(bases.status === "fulfilled" ? bases.value.bases : []);
      },
    );
  };

  const upload = async (file: File) => {
    if (!pickerKind || pickerKind === "tool") {
      toast.info(t("team.resources.uploadUnsupported"));
      return;
    }
    try {
      const kind =
        pickerKind === "skill" ? "sop" : pickerKind === "general_skill" ? "skill" : "knowledge";
      const baseId =
        pickerKind === "knowledge_base"
          ? (knowledgeBases.find((base) => base.employee_id === employeeId)?.base_id ??
            `emp-${employeeId}`)
          : undefined;
      await teamApi.importResource(kind, file, {
        baseId,
        employeeId: pickerKind === "knowledge_base" ? employeeId : undefined,
      });
      toast.success(t("team.resources.uploaded"));
      reloadCandidates();
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.resources.uploadFailed")));
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {GROUPS.map((group) => {
        const items = boundByKind.get(group.kind) ?? [];
        const Icon = group.icon;
        return (
          <section key={group.kind} className="bg-card border-chat-hairline rounded-xl border p-4">
            <div className="flex items-center justify-between">
              <div className="text-chat-ink flex items-center gap-2 text-xs font-semibold">
                <Icon className="text-chat-muted size-3.5" aria-hidden="true" />
                {t(group.labelKey)}
                <span className="text-chat-muted font-normal">({items.length})</span>
              </div>
              <Button variant="ghost" size="sm" onClick={() => openPicker(group.kind)}>
                <Plus />
                {t("team.resources.bind")}
              </Button>
            </div>
            {items.length === 0 ? (
              <p className="text-chat-muted mt-2 text-[11px]">{t("team.resources.empty")}</p>
            ) : (
              <ul className="mt-2 flex flex-wrap gap-2">
                {items.map((item) => (
                  <li
                    key={`${item.resource_type}:${item.resource_id}`}
                    className={cn(
                      "bg-primary/[0.03] text-chat-ink inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-[11px]",
                      item.status !== "active" && "opacity-50",
                    )}
                  >
                    {displayName(item, knowledge)}
                    {item.status !== "active" ? (
                      <span className="text-chat-muted">({t("team.resources.inactive")})</span>
                    ) : null}
                    <button
                      type="button"
                      aria-label={t("team.resources.unbind")}
                      onClick={() => void unbind(group.kind, item.resource_id)}
                      className="text-chat-muted hover:text-chat-ink"
                    >
                      <X className="size-3" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}

      <ResourcePickerDialog
        open={pickerKind !== null}
        title={`${t("team.resources.pickTitle")} · ${t(
          GROUPS.find((group) => group.kind === pickerKind)?.labelKey ?? "team.resources.skills",
        )}`}
        items={candidates}
        selectedIds={selectedIds}
        submitting={submitting}
        onOpenChange={(open) => !open && setPickerKind(null)}
        onToggle={toggle}
        onSubmit={() => void submit()}
        onUpload={(file) => void upload(file)}
      />
    </div>
  );
}

/** 功能：资源展示名（知识库取档案名，其余用资源 id）。 */
function displayName(item: EmployeeResource, knowledge: EmployeeKnowledgeBase[]): string {
  if (item.resource_type === "knowledge_base") {
    const base = knowledge.find((entry) => entry.base_id === item.resource_id);
    const metaName = item.metadata?.name;
    if (base) return base.name;
    if (typeof metaName === "string" && metaName) return metaName;
  }
  return item.resource_id;
}
