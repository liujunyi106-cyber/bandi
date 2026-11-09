/**
 * 功能：员工档案「知识与记忆」Tab（知识库绑定/上传/单个移除 + 记忆写审批）。
 * 知识库与技能是可复用资源：绑定只解除当前员工，不删库本身。
 */
import { Check, Plus, Upload, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { ResourcePickerDialog } from "@/components/team/ResourcePickerDialog";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { api } from "@/services/api";
import type { PendingMemory } from "@/components/team/EmployeeProfileSheet";
import type { EmployeeSummary } from "@/services/company";
import {
  describeError,
  teamApi,
  type EmployeeKnowledgeBase,
  type EmployeeProfile,
  type KnowledgeBaseOption,
} from "@/services/team";

export interface EmployeeMemoryTabProps {
  employee: EmployeeSummary;
  knowledge: EmployeeKnowledgeBase[];
  pending: PendingMemory[];
  profile: EmployeeProfile | null;
  onChanged: () => void;
}

/** 功能：知识与记忆浏览区（知识库复用资源选择/上传，记忆写入单独审批）。 */
export function EmployeeMemoryTab({
  employee,
  knowledge,
  pending,
  profile,
  onChanged,
}: EmployeeMemoryTabProps) {
  const t = useT();
  const [rows, setRows] = useState<PendingMemory[]>(pending);
  const [busyId, setBusyId] = useState("");
  const [bases, setBases] = useState<KnowledgeBaseOption[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setRows(pending);
  }, [pending]);

  useEffect(() => {
    let cancelled = false;
    teamApi
      .knowledgeBases()
      .then((data) => {
        if (!cancelled) setBases(data.bases ?? []);
      })
      .catch(() => {
        if (!cancelled) setBases([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const boundIds = useMemo(
    () =>
      (profile?.resources ?? [])
        .filter((item) => item.resource_type === "knowledge_base")
        .map((item) => item.resource_id),
    [profile],
  );

  const nameOf = (baseId: string) =>
    bases.find((base) => base.base_id === baseId)?.name ||
    knowledge.find((base) => base.base_id === baseId)?.name ||
    baseId;

  const docCountOf = (baseId: string) =>
    bases.find((base) => base.base_id === baseId)?.document_count ??
    knowledge.find((base) => base.base_id === baseId)?.document_count ??
    0;

  const options = useMemo(
    () =>
      bases.map((base) => ({
        id: base.base_id,
        label: base.name,
        hint: `${base.document_count} ${t("team.resources.documents")}`,
      })),
    [bases, t],
  );

  const unbind = async (baseId: string) => {
    setBusyId(baseId);
    try {
      await teamApi.unbindResource(employee.employee_id, "knowledge_base", baseId);
      toast.success(t("team.memory.removed").replace("{name}", nameOf(baseId)));
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.memory.removeFailed")));
    } finally {
      setBusyId("");
    }
  };

  const bindSelected = async () => {
    if (selected.length === 0) return;
    setSubmitting(true);
    try {
      for (const baseId of selected) {
        await teamApi.bindResource(employee.employee_id, "knowledge_base", baseId);
      }
      toast.success(t("team.memory.bound").replace("{count}", String(selected.length)));
      setSelected([]);
      setPickerOpen(false);
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.memory.bindFailed")));
    } finally {
      setSubmitting(false);
    }
  };

  const upload = async (file: File) => {
    setSubmitting(true);
    try {
      await teamApi.importResource("knowledge_base", file, { employeeId: employee.employee_id });
      toast.success(t("team.memory.uploaded").replace("{name}", file.name));
      setPickerOpen(false);
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.memory.uploadFailed")));
    } finally {
      setSubmitting(false);
    }
  };

  const decide = async (id: string, approve: boolean) => {
    setBusyId(id);
    try {
      await api.post(`/memory/pending/${encodeURIComponent(id)}/${approve ? "approve" : "reject"}`);
      setRows((current) => current.filter((row) => row.id !== id));
      toast.success(approve ? t("team.memory.approved") : t("team.memory.rejected"));
    } catch (error) {
      toast.error(describeError(error, t("team.memory.decideFailed")));
    } finally {
      setBusyId("");
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <section className="bg-card border-chat-hairline rounded-xl border p-4">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-chat-ink text-xs font-semibold">
            {t("team.memory.knowledgeTitle")}
            <span className="text-chat-muted ml-1.5 text-[10px] font-normal">
              {boundIds.length} {t("team.memory.bases")}
            </span>
          </h3>
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2 text-[11px]"
              data-testid="memory-kb-bind"
              onClick={() => {
                setSelected([]);
                setPickerOpen(true);
              }}
            >
              <Plus className="size-3" />
              {t("team.memory.bindAction")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2 text-[11px]"
              data-testid="memory-kb-upload"
              onClick={() => fileRef.current?.click()}
            >
              <Upload className="size-3" />
              {t("team.memory.uploadAction")}
            </Button>
            <input
              ref={fileRef}
              type="file"
              className="hidden"
              accept=".md,.txt,.json,.csv"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) void upload(file);
              }}
            />
          </div>
        </div>

        {boundIds.length === 0 ? (
          <p className="text-chat-muted mt-2 text-[11px]">{t("team.memory.knowledgeEmpty")}</p>
        ) : (
          <ul className="mt-3 flex flex-col gap-2">
            {boundIds.map((baseId) => (
              <li
                key={baseId}
                className="border-chat-hairline flex items-center justify-between gap-3 rounded-lg border px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="text-chat-ink truncate text-xs">{nameOf(baseId)}</p>
                  <p className="text-chat-muted mt-0.5 text-[11px]">
                    {baseId} · {docCountOf(baseId)} {t("team.resources.documents")}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="text-chat-muted hover:text-destructive h-7 shrink-0 px-2 text-[11px]"
                  disabled={busyId === baseId}
                  data-testid={`memory-kb-remove-${baseId}`}
                  onClick={() => void unbind(baseId)}
                >
                  {t("team.memory.remove")}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="bg-card border-chat-hairline rounded-xl border p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-chat-ink text-xs font-semibold">{t("team.memory.pendingTitle")}</h3>
          <span className="text-chat-muted text-[11px]">
            {rows.length} {t("team.memory.pendingCount")}
          </span>
        </div>
        {rows.length === 0 ? (
          <p className="text-chat-muted mt-2 text-[11px]">{t("team.memory.pendingEmpty")}</p>
        ) : (
          <ul className="mt-3 flex flex-col gap-2">
            {rows.map((row) => (
              <li key={row.id} className="border-chat-hairline rounded-lg border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-chat-ink line-clamp-2 text-xs leading-5">{row.summary}</p>
                    <p className="text-chat-muted mt-1 text-[11px]">
                      {row.subsystem} · {row.action}
                      {row.conflict ? ` · ${t("team.memory.conflict")}` : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busyId === row.id}
                      onClick={() => void decide(row.id, true)}
                    >
                      <Check />
                      {t("team.memory.approve")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busyId === row.id}
                      onClick={() => void decide(row.id, false)}
                    >
                      <X />
                      {t("team.memory.reject")}
                    </Button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
        <p className="text-chat-muted mt-3 text-[11px]">
          {t("team.memory.scopeHint")} {employee.name}
        </p>
      </section>

      <ResourcePickerDialog
        open={pickerOpen}
        title={`${t("team.resources.pickTitle")} · ${t("team.resources.knowledge")}`}
        items={options}
        selectedIds={selected}
        submitting={submitting}
        uploadLabel={t("team.memory.uploadAction")}
        onOpenChange={setPickerOpen}
        onToggle={(id) =>
          setSelected((current) =>
            current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
          )
        }
        onSubmit={() => void bindSelected()}
        onUpload={(file) => void upload(file)}
      />
    </div>
  );
}
