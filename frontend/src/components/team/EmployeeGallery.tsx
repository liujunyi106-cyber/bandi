/**
 * 功能：团队页（全部 / 下线 / 组织编排 三视图切换；状态统计 / 搜索 / 卡片网格 / 空态）。
 * 添加员工入口位于统计区右侧；退役员工等同删除，不在列表展示。
 */
import {
  ChevronDown,
  Download,
  Plus,
  Search,
  Sparkles,
  Trash2,
  UserPlus,
  UsersRound,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { AddRoleToTeamDialog } from "@/components/team/AddRoleToTeamDialog";
import { CreateTeamDialog } from "@/components/team/CreateTeamDialog";
import { EmployeeCard } from "@/components/team/EmployeeCard";
import { OrgChartView } from "@/components/team/OrgChartView";
import { TemplateGallery } from "@/components/team/TemplateGallery";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { EmployeeSummary, OrgTeamSummary } from "@/services/company";
import {
  describeError,
  EMPTY_EMPLOYEE_STATS,
  teamApi,
  teamNameOf,
  type EmployeeStats,
  type TeamOverview,
} from "@/services/team";

export type GalleryTab = "all" | "offline" | "org" | "tpl";

export interface EmployeeGalleryProps {
  overview: TeamOverview | null;
  stats: Record<string, EmployeeStats>;
  loading: boolean;
  error: string;
  onAdd: () => void;
  onUseExample: () => void;
  onOpenEmployee: (employee: EmployeeSummary) => void;
  onChat: (employee: EmployeeSummary) => void;
  onChanged: () => void;
  onEdit: (employee: EmployeeSummary) => void;
  onToggleStatus: (employee: EmployeeSummary) => void;
  onRetire: (employee: EmployeeSummary) => void;
}

/** 功能：全部视图含在线与下线；下线视图仅暂停员工。 */
function matchesTab(employee: EmployeeSummary, tab: GalleryTab): boolean {
  const status = employee.status ?? "active";
  if (tab === "all") return true;
  return status === "paused";
}

/** 功能：画廊排序（在线优先，其次下线；同状态按姓名）。 */
const STATUS_ORDER: Record<string, number> = {
  active: 0,
  paused: 1,
};

export function EmployeeGallery({
  overview,
  stats,
  loading,
  error,
  onAdd,
  onUseExample,
  onOpenEmployee,
  onChat,
  onChanged,
  onEdit,
  onToggleStatus,
  onRetire,
}: EmployeeGalleryProps) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<GalleryTab>("all");
  const [selectedTeamId, setSelectedTeamId] = useState("");
  const [teamQuery, setTeamQuery] = useState("");
  const [teamMenuOpen, setTeamMenuOpen] = useState(false);
  const [addRoleOpen, setAddRoleOpen] = useState(false);
  const [createTeamOpen, setCreateTeamOpen] = useState(false);
  const [dissolveTarget, setDissolveTarget] = useState<OrgTeamSummary | null>(null);
  const pendingTeamRef = useRef("");

  const employees = useMemo(
    () => (overview?.employees ?? []).filter((employee) => employee.status !== "retired"),
    [overview],
  );
  const roles = useMemo(() => overview?.roles ?? [], [overview]);
  const orgTeams = useMemo(() => overview?.orgTeams ?? [], [overview]);

  useEffect(() => {
    if (orgTeams.length === 0) {
      setSelectedTeamId("");
      return;
    }
    if (pendingTeamRef.current && orgTeams.some((team) => team.team_id === pendingTeamRef.current)) {
      setSelectedTeamId(pendingTeamRef.current);
      pendingTeamRef.current = "";
      return;
    }
    if (!orgTeams.some((team) => team.team_id === selectedTeamId)) {
      setSelectedTeamId(orgTeams[0].team_id);
    }
  }, [orgTeams, selectedTeamId]);

  const currentTeam = orgTeams.find((team) => team.team_id === selectedTeamId) ?? null;
  const filteredTeams = useMemo(() => {
    const keyword = teamQuery.trim().toLowerCase();
    if (!keyword) return orgTeams;
    return orgTeams.filter((team) =>
      [team.name, team.team_id].some((value) => value.toLowerCase().includes(keyword)),
    );
  }, [orgTeams, teamQuery]);

  const statsSummary = useMemo(() => {
    const active = employees.filter(
      (employee) => (employee.status ?? "active") === "active",
    ).length;
    return {
      total: employees.length,
      offline: employees.length - active,
      teams: orgTeams.length,
    };
  }, [employees, orgTeams.length]);

  const visible = useMemo(() => {
    if (tab === "org" || tab === "tpl") return [];
    const keyword = query.trim().toLowerCase();
    return employees
      .filter((employee) => {
        if (!matchesTab(employee, tab)) return false;
        if (!keyword) return true;
        const roleName = roles.find((role) => role.id === employee.role_id)?.name ?? "";
        const teamName = teamNameOf(employee, orgTeams);
        return [
          employee.name,
          employee.role_id,
          roleName,
          teamName,
          employee.description ?? "",
        ].some((value) => value.toLowerCase().includes(keyword));
      })
      .sort((left, right) => {
        const delta =
          (STATUS_ORDER[left.status ?? "active"] ?? 1) -
          (STATUS_ORDER[right.status ?? "active"] ?? 1);
        return delta !== 0 ? delta : left.name.localeCompare(right.name, "zh-Hans-CN");
      });
  }, [employees, orgTeams, query, roles, tab]);

  const summary = [
    { key: "total", value: statsSummary.total, label: t("team.stats.total") },
    { key: "offline", value: statsSummary.offline, label: t("team.stats.offline") },
    { key: "teams", value: statsSummary.teams, label: t("team.stats.teams") },
  ];

  const exportTeam = async (team: OrgTeamSummary) => {
    try {
      const filename = await teamApi.exportOrgTeam(team.team_id);
      toast.success(t("team.export.done").replace("{name}", filename));
    } catch (error) {
      toast.error(describeError(error, t("team.export.failed")));
    }
  };

  const dissolveTeam = async () => {
    const team = dissolveTarget;
    if (!team) return;
    try {
      await teamApi.dissolveTeam(team.team_id);
      toast.success(t("team.dissolved"));
      setDissolveTarget(null);
      if (selectedTeamId === team.team_id) {
        setSelectedTeamId(orgTeams.find((item) => item.team_id !== team.team_id)?.team_id ?? "");
      }
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.dissolveFailed")));
    }
  };

  const tabs = [
    { value: "all", label: t("team.filter.all") },
    { value: "offline", label: t("team.filter.offline") },
    { value: "org", label: t("team.filter.org") },
    { value: "tpl", label: t("team.filter.templates") },
  ];

  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-6 px-8 pt-9 pb-7">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-chat-ink text-xl font-semibold">{t("team.title")}</h1>
          <p className="text-chat-muted mt-1 text-xs">{t("team.subtitle")}</p>
        </div>
      </header>

      <div className="flex min-h-9 flex-wrap items-center justify-between gap-3">
        <SegmentedTabs
          items={tabs}
          value={tab}
          onChange={(value) => setTab(value as GalleryTab)}
          ariaLabel={t("team.filter.label")}
        />
        {tab === "org" ? (
          <div className="flex items-center gap-2">
            <Popover open={teamMenuOpen} onOpenChange={setTeamMenuOpen}>
              <PopoverTrigger asChild>
                <Button variant="outline" className="text-xs" data-testid="team-picker-trigger">
                  <UsersRound />
                  {currentTeam?.name ?? t("team.manageTeams")}
                  <ChevronDown className="size-3.5" />
                </Button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-64 p-2">
                <Input
                  value={teamQuery}
                  onChange={(event) => setTeamQuery(event.target.value)}
                  placeholder={t("team.searchTeams")}
                  className="h-8 text-xs"
                />
                <div className="mt-2 max-h-60 overflow-y-auto">
                  {filteredTeams.length === 0 ? (
                    <p className="text-chat-muted px-2 py-6 text-center text-[11px]">
                      {t("team.noTeams")}
                    </p>
                  ) : (
                    filteredTeams.map((team) => (
                      <div
                        key={team.team_id}
                        className={cn(
                          "hover:bg-primary/5 flex w-full items-center gap-1 rounded",
                          team.team_id === selectedTeamId && "bg-primary/5",
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => {
                            setSelectedTeamId(team.team_id);
                            setTeamQuery("");
                            setTeamMenuOpen(false);
                          }}
                          className="flex min-w-0 flex-1 items-center justify-between px-2 py-2 text-left"
                        >
                          <span className="text-chat-ink truncate text-xs">{team.name}</span>
                          <span className="text-chat-muted text-[10px]">{team.role_count}</span>
                        </button>
                        <button
                          type="button"
                          data-testid={`team-export-${team.team_id}`}
                          aria-label={`${t("team.export.action")} ${team.name}`}
                          title={t("team.export.action")}
                          className="text-chat-muted hover:text-chat-ink flex size-7 shrink-0 items-center justify-center rounded"
                          onClick={() => void exportTeam(team)}
                        >
                          <Download className="size-3.5" />
                        </button>
                        <button
                          type="button"
                          data-testid={`team-dissolve-${team.team_id}`}
                          aria-label={`${t("team.dissolve")} ${team.name}`}
                          title={t("team.dissolve")}
                          className="text-chat-muted hover:text-destructive flex size-7 shrink-0 items-center justify-center rounded"
                          onClick={() => setDissolveTarget(team)}
                        >
                          <Trash2 className="size-3.5" />
                        </button>
                      </div>
                    ))
                  )}
                </div>
              </PopoverContent>
            </Popover>
            <Button
              variant="outline"
              className="text-xs"
              disabled={!selectedTeamId}
              onClick={() => setAddRoleOpen(true)}
            >
              <UserPlus />
              {t("team.addRole")}
            </Button>
            <Button className="text-xs" onClick={() => setCreateTeamOpen(true)}>
              <Plus />
              {t("team.createTeam")}
            </Button>
          </div>
        ) : tab === "tpl" ? null : (
          <label className="border-chat-hairline bg-card flex h-8 w-[260px] items-center gap-2 rounded-lg border px-3">
            <Search className="text-chat-muted size-3.5 shrink-0" aria-hidden="true" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("team.searchPlaceholder")}
              aria-label={t("team.searchPlaceholder")}
              className="h-full min-w-0 flex-1 border-0 bg-transparent px-0 text-xs shadow-none"
            />
          </label>
        )}
      </div>

      {tab === "org" ? (
        <OrgChartView
          roles={roles}
          orgTeams={orgTeams}
          employees={employees}
          selectedTeamId={selectedTeamId}
          onChanged={onChanged}
        />
      ) : tab === "tpl" ? (
        <TemplateGallery
          templates={overview?.templates ?? []}
          orgTeams={orgTeams}
          roleIds={roles.map((role) => role.id)}
          onApplied={() => {
            setTab("all");
            onChanged();
          }}
        />
      ) : (
        <div className="flex min-h-[600px] flex-col gap-6">
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {summary.map((item) => (
              <div
                key={item.key}
                className="bg-card border-chat-hairline flex items-center gap-3 rounded-xl border px-4 py-3"
              >
                <strong className="text-chat-ink text-2xl font-semibold">{item.value}</strong>
                <span className="text-chat-muted text-xs">{item.label}</span>
              </div>
            ))}
            <button
              type="button"
              onClick={onAdd}
              className="bg-primary text-primary-foreground hover:bg-primary/90 flex items-center gap-3 rounded-xl px-4 py-3 text-left transition-colors"
            >
              <Plus className="size-5 shrink-0" />
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium">{t("team.addEmployee")}</span>
                <span className="text-primary-foreground/70 block truncate text-[10px]">
                  {t("team.addEmployeeHint")}
                </span>
              </span>
            </button>
          </section>

          {error ? (
            <div className="border-chat-hairline text-chat-muted rounded-xl border border-dashed px-6 py-12 text-center text-xs">
              {error}
            </div>
          ) : loading ? (
            <div className="text-chat-muted px-6 py-12 text-center text-xs">
              {t("team.loading")}
            </div>
          ) : visible.length === 0 ? (
            <div
              data-testid="team-empty-state"
              className={cn(
                "border-chat-hairline flex flex-col items-center justify-center rounded-2xl border border-dashed px-6 py-14 text-center",
                "bg-primary/[0.01]",
              )}
            >
              <span className="bg-primary/5 text-chat-muted grid size-12 place-items-center rounded-2xl">
                <UsersRound className="size-5" aria-hidden="true" />
              </span>
              <p className="text-chat-ink mt-4 text-sm font-medium">
                {employees.length === 0
                  ? t("team.empty.title")
                  : tab === "offline"
                    ? t("team.empty.offlineTitle")
                    : t("team.empty.searchTitle")}
              </p>
              <p className="text-chat-muted mt-1 max-w-[360px] text-xs leading-5">
                {employees.length === 0
                  ? t("team.empty.description")
                  : t("team.empty.searchDescription")}
              </p>
              {employees.length === 0 ? (
                <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
                  <Button onClick={onUseExample}>
                    <Sparkles />
                    {t("team.empty.useExample")}
                  </Button>
                  <Button variant="outline" onClick={onAdd}>
                    <Plus />
                    {t("team.addEmployee")}
                  </Button>
                </div>
              ) : null}
            </div>
          ) : (
            <section
              aria-busy={loading}
              className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4"
            >
              {visible.map((employee) => (
                <EmployeeCard
                  key={employee.employee_id}
                  employee={employee}
                  stats={stats[employee.employee_id] ?? EMPTY_EMPLOYEE_STATS}
                  onOpen={() => onOpenEmployee(employee)}
                  onChat={() => onChat(employee)}
                  onEdit={() => onEdit(employee)}
                  onToggleStatus={() => onToggleStatus(employee)}
                  onRetire={() => onRetire(employee)}
                />
              ))}
            </section>
          )}
        </div>
      )}

      <AddRoleToTeamDialog
        open={addRoleOpen}
        teamId={selectedTeamId}
        overview={overview}
        onOpenChange={setAddRoleOpen}
        onChanged={onChanged}
      />

      <CreateTeamDialog
        open={createTeamOpen}
        overview={overview}
        onOpenChange={setCreateTeamOpen}
        onCreated={(teamId) => {
          pendingTeamRef.current = teamId;
          onChanged();
        }}
      />

      <AlertDialog
        open={dissolveTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDissolveTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("team.dissolve")}</AlertDialogTitle>
            <AlertDialogDescription>
              {dissolveTarget ? `${dissolveTarget.name} · ` : ""}
              {t("team.dissolveConfirm")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => void dissolveTeam()}
            >
              {t("team.dissolve")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
