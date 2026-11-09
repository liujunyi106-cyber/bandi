/**
 * 功能：员工档案右侧抽屉（人设 / 技能与资源 / 知识与记忆 / 状态与组织）。
 * 进入时加载档案、知识库与待审记忆；人设保存走员工更新端点并回刷画廊。
 */
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { EmployeeMemoryTab } from "@/components/team/EmployeeMemoryTab";
import { EmployeeOrgTab } from "@/components/team/EmployeeOrgTab";
import { EmployeePersonaTab } from "@/components/team/EmployeePersonaTab";
import { EmployeeResourcesTab } from "@/components/team/EmployeeResourcesTab";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { useT } from "@/i18n";
import type { EmployeeAvatarMeta } from "@/lib/employee-avatar";
import type { EmployeeSummary } from "@/services/company";
import {
  describeError,
  teamApi,
  teamNameOf,
  type EmployeeKnowledgeBase,
  type EmployeeProfile,
  type TeamOverview,
} from "@/services/team";

export type ProfileTab = "persona" | "resources" | "memory" | "org";

/** 待审记忆（GET /api/memory/pending 子集）。 */
export interface PendingMemory {
  id: string;
  subsystem: string;
  action: string;
  summary: string;
  origin: string;
  created_at: string;
  conflict: boolean;
}

interface ProfileState {
  status: "idle" | "loading" | "ready" | "error";
  profile: EmployeeProfile | null;
  knowledge: EmployeeKnowledgeBase[];
  pending: PendingMemory[];
  error: string;
}

const EMPTY_PROFILE: ProfileState = {
  status: "idle",
  profile: null,
  knowledge: [],
  pending: [],
  error: "",
};

export interface EmployeeProfileSheetProps {
  employee: EmployeeSummary | null;
  overview: TeamOverview | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUpdated: () => void;
  initialTab?: ProfileTab;
}

/** 功能：员工档案抽屉外壳与四个 Tab 的加载编排。 */
export function EmployeeProfileSheet({
  employee,
  overview,
  open,
  onOpenChange,
  onUpdated,
  initialTab = "persona",
}: EmployeeProfileSheetProps) {
  const t = useT();
  const [tab, setTab] = useState<ProfileTab>(initialTab);
  const [state, setState] = useState<ProfileState>(EMPTY_PROFILE);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    if (!open) return;
    setTab(initialTab);
  }, [initialTab, open]);

  useEffect(() => {
    if (!open || !employee) {
      setState(EMPTY_PROFILE);
      return;
    }
    let cancelled = false;
    setState((current) => ({ ...current, status: "loading", error: "" }));
    const employeeId = employee.employee_id;
    Promise.allSettled([
      teamApi.profile(employeeId),
      teamApi.knowledge(employeeId),
      teamApi.pendingMemories(),
    ]).then(([profileResult, knowledgeResult, pendingResult]) => {
      if (cancelled) return;
      if (profileResult.status === "rejected") {
        setState({
          status: "error",
          profile: null,
          knowledge: [],
          pending: [],
          error: describeError(profileResult.reason, t("team.profile.loadFailed")),
        });
        return;
      }
      setState({
        status: "ready",
        profile: profileResult.value.profile,
        knowledge: knowledgeResult.status === "fulfilled" ? knowledgeResult.value.bases : [],
        pending:
          pendingResult.status === "fulfilled"
            ? (pendingResult.value.pending as PendingMemory[])
            : [],
        error: "",
      });
    });
    return () => {
      cancelled = true;
    };
  }, [employee, open, reloadToken, t]);

  if (!employee) return null;

  const role = overview?.roles.find((item) => item.id === employee.role_id);
  const teamName = teamNameOf(employee, overview?.orgTeams ?? []);
  const status = employee.status ?? "active";
  const savePersona = async (patch: {
    name: string;
    description: string;
    avatar: EmployeeAvatarMeta;
    llm_config: { default_model?: string; thinking_mode?: string } | null;
  }) => {
    try {
      const { avatar, ...profilePatch } = patch;
      await teamApi.updateEmployee(employee.employee_id, {
        ...profilePatch,
        metadata: {
          ...employee.metadata,
          persona_prompt: patch.description,
          avatar,
        },
      });
      toast.success(t("team.profile.saved"));
      onUpdated();
    } catch (error) {
      toast.error(describeError(error, t("team.profile.saveFailed")));
      throw error;
    }
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="flex w-full flex-col gap-0 p-0 data-[side=right]:sm:max-w-[720px]"
        aria-describedby={undefined}
      >
        <header className="border-chat-hairline border-b px-6 pt-5 pb-4">
          <div className="flex items-start gap-3">
            <EmployeeAvatar
              name={employee.name}
              metadata={employee.metadata}
              className="size-12 text-lg"
            />
            <div className="min-w-0 flex-1">
              <SheetTitle className="text-chat-ink truncate text-base font-semibold">
                {employee.name}
              </SheetTitle>
              <SheetDescription className="text-chat-muted mt-0.5 text-xs">
                {role?.name ?? employee.role_id}
                {teamName ? ` · ${teamName}` : ""}
              </SheetDescription>
              <div className="text-chat-muted mt-2 flex flex-wrap items-center gap-3 text-[11px]">
                <span>{t(`team.status.${status}`)}</span>
                <span>
                  {t("team.profile.skills")} {employee.skills?.length ?? 0}
                </span>
              </div>
            </div>
          </div>
        </header>

        <div className="border-chat-hairline border-b px-6 py-2">
          <SegmentedTabs
            items={[
              { value: "persona", label: t("team.tab.persona") },
              { value: "resources", label: t("team.tab.resources") },
              { value: "memory", label: t("team.tab.memory") },
              { value: "org", label: t("team.tab.org") },
            ]}
            value={tab}
            onChange={(value) => setTab(value as ProfileTab)}
            ariaLabel={t("team.tab.label")}
          />
        </div>

        <div className="min-h-0 w-full flex-1 overflow-y-auto px-6 py-5">
          {state.status === "loading" ? (
            <div className="text-chat-muted flex items-center gap-2 py-10 text-xs">
              <Loader2 className="size-3.5 animate-spin" />
              {t("team.profile.loading")}
            </div>
          ) : state.status === "error" ? (
            <div className="text-chat-muted py-10 text-xs">{state.error}</div>
          ) : tab === "persona" ? (
            <EmployeePersonaTab
              employee={employee}
              roleName={role?.name ?? ""}
              models={overview?.models?.available_models ?? []}
              globalModel={overview?.models?.effective_model ?? ""}
              onSave={savePersona}
            />
          ) : tab === "resources" ? (
            <EmployeeResourcesTab
              employeeId={employee.employee_id}
              profile={state.profile}
              knowledge={state.knowledge}
              overview={overview}
              onChanged={() => {
                setReloadToken((value) => value + 1);
                onUpdated();
              }}
            />
          ) : tab === "memory" ? (
            <EmployeeMemoryTab
              employee={employee}
              knowledge={state.knowledge}
              pending={state.pending}
              profile={state.profile}
              onChanged={() => {
                setReloadToken((value) => value + 1);
                onUpdated();
              }}
            />
          ) : (
            <EmployeeOrgTab
              employee={employee}
              roles={overview?.roles ?? []}
              orgTeams={overview?.orgTeams ?? []}
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
