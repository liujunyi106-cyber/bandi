/**
 * 功能：创建团队弹窗（左侧选员工、右侧已选成员与上级、右侧底部团队名称）。
 * 至少两名员工；创建后团队归属写入员工岗位并注册中文团队名。
 */
import { Check, ChevronDown, Plus, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
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
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { EmployeeSummary } from "@/services/company";
import { describeError, teamApi, type TeamOverview } from "@/services/team";

const NO_MANAGER = "__none__";

export interface CreateTeamDialogProps {
  open: boolean;
  overview: TeamOverview | null;
  onOpenChange: (open: boolean) => void;
  onCreated: (teamId: string) => void;
}

/** 功能：创建团队。 */
export function CreateTeamDialog({
  open,
  overview,
  onOpenChange,
  onCreated,
}: CreateTeamDialogProps) {
  const t = useT();
  const [name, setName] = useState("");
  const [query, setQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [managers, setManagers] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const employees = useMemo(
    () => (overview?.employees ?? []).filter((employee) => employee.status !== "retired"),
    [overview?.employees],
  );

  useEffect(() => {
    if (!open) return;
    setName("");
    setQuery("");
    setSelectedIds([]);
    setManagers({});
  }, [open]);

  const filteredEmployees = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) return employees;
    return employees.filter((employee) =>
      [employee.name, employee.description ?? "", employee.role_id].some((value) =>
        value.toLowerCase().includes(keyword),
      ),
    );
  }, [employees, query]);

  const selectedEmployees = useMemo(
    () =>
      selectedIds
        .map((id) => employees.find((employee) => employee.employee_id === id))
        .filter((employee): employee is EmployeeSummary => Boolean(employee)),
    [employees, selectedIds],
  );

  const toggle = (employeeId: string) => {
    setSelectedIds((current) => {
      if (current.includes(employeeId)) {
        setManagers((existing) => {
          const next = { ...existing };
          delete next[employeeId];
          return next;
        });
        return current.filter((item) => item !== employeeId);
      }
      return [...current, employeeId];
    });
  };

  const setManager = (employeeId: string, managerId: string) => {
    setManagers((current) => ({
      ...current,
      [employeeId]: managerId === NO_MANAGER ? "" : managerId,
    }));
  };

  const submit = async () => {
    if (!name.trim() || selectedIds.length < 2) return;
    setSaving(true);
    try {
      const result = await teamApi.createTeam({
        name: name.trim(),
        members: selectedEmployees.map((employee) => ({
          employee_id: employee.employee_id,
          reports_to: managers[employee.employee_id] ?? "",
        })),
      });
      toast.success(t("team.create.created"));
      onCreated(result.team_id);
      onOpenChange(false);
    } catch (error) {
      toast.error(describeError(error, t("team.create.failed")));
    } finally {
      setSaving(false);
    }
  };

  const canSubmit = Boolean(name.trim()) && selectedIds.length >= 2 && !saving;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[620px] max-h-[calc(100dvh-4rem)] flex-col gap-4 sm:max-w-[880px]">
        <DialogHeader>
          <DialogTitle className="text-foreground text-lg font-semibold">
            {t("team.create.title")}
          </DialogTitle>
          <DialogDescription className="text-chat-muted text-[11px]">
            {t("team.create.subtitle")}
          </DialogDescription>
        </DialogHeader>

        <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 md:grid-cols-2">
          <section className="border-chat-hairline flex min-h-0 flex-col rounded-xl border p-3">
            <div className="border-chat-hairline flex h-8 items-center gap-2 rounded-full border px-3">
              <Search className="text-chat-muted size-3.5 shrink-0" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("team.create.search")}
                className="h-full min-w-0 flex-1 rounded-none border-0 bg-transparent px-0 text-xs shadow-none"
              />
            </div>
            <div className="mt-2 min-h-0 flex-1 overflow-y-auto">
              {filteredEmployees.length === 0 ? (
                <p className="text-chat-muted px-2 py-8 text-center text-[11px]">
                  {t("team.create.empty")}
                </p>
              ) : (
                filteredEmployees.map((employee) => {
                  const active = selectedIds.includes(employee.employee_id);
                  return (
                    <button
                      key={employee.employee_id}
                      type="button"
                      onClick={() => toggle(employee.employee_id)}
                      className="hover:bg-primary/5 flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left"
                    >
                      <EmployeeAvatar
                        name={employee.name}
                        metadata={employee.metadata}
                        className="size-8 rounded-full text-[11px]"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="text-chat-ink block truncate text-xs font-medium">
                          {employee.name}
                        </span>
                        <span className="text-chat-muted mt-0.5 block truncate text-[10px]">
                          {employee.description ||
                            String(employee.metadata?.persona_prompt ?? "") ||
                            t("team.card.noDescription")}
                        </span>
                      </span>
                      <span
                        className={cn(
                          "grid size-6 shrink-0 place-items-center rounded-md border",
                          active
                            ? "border-primary bg-primary text-primary-foreground"
                            : "border-chat-hairline text-chat-muted",
                        )}
                      >
                        {active ? <Check className="size-3.5" /> : <Plus className="size-3.5" />}
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          </section>

          <section className="border-chat-hairline flex min-h-0 flex-col rounded-xl border p-3">
            <div className="flex items-center justify-between">
              <h3 className="text-chat-muted text-[11px] font-medium">
                {t("team.create.selectedTitle")}
              </h3>
              <span className="text-chat-muted text-[10px]">{selectedEmployees.length}</span>
            </div>
            <div className="mt-2 min-h-0 flex-1 overflow-y-auto">
              {selectedEmployees.length === 0 ? (
                <p className="text-chat-muted px-2 py-8 text-center text-[11px]">
                  {t("team.create.minMembers")}
                </p>
              ) : (
                selectedEmployees.map((employee) => {
                  const managerId = managers[employee.employee_id] ?? "";
                  const managerName =
                    selectedEmployees.find((item) => item.employee_id === managerId)?.name ?? "";
                  return (
                    <div
                      key={employee.employee_id}
                      className="border-chat-hairline mb-2 flex items-center gap-2 rounded-lg border px-2 py-2"
                    >
                      <EmployeeAvatar
                        name={employee.name}
                        metadata={employee.metadata}
                        className="size-8 rounded-full text-[11px]"
                      />
                      <div className="min-w-0 flex-1">
                        <p className="text-chat-ink truncate text-xs font-medium">
                          {employee.name}
                        </p>
                        <p className="text-chat-muted mt-0.5 line-clamp-1 text-[10px]">
                          {employee.description ||
                            String(employee.metadata?.persona_prompt ?? "") ||
                            t("team.card.noDescription")}
                        </p>
                      </div>
                      <Popover>
                        <PopoverTrigger asChild>
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-6 max-w-[120px] shrink-0 gap-1 px-2 text-[10px]"
                          >
                            <span className="truncate">
                              {managerName
                                ? `${t("team.create.manager")} · ${managerName}`
                                : t("team.create.manager")}
                            </span>
                            <ChevronDown className="size-3 shrink-0" />
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent align="end" className="w-48 p-1">
                          <button
                            type="button"
                            onClick={() => setManager(employee.employee_id, NO_MANAGER)}
                            className="hover:bg-primary/5 flex w-full items-center rounded px-2 py-1.5 text-left text-[11px]"
                          >
                            {t("team.create.noManager")}
                          </button>
                          {selectedEmployees
                            .filter((item) => item.employee_id !== employee.employee_id)
                            .map((item) => (
                              <button
                                key={item.employee_id}
                                type="button"
                                onClick={() => setManager(employee.employee_id, item.employee_id)}
                                className="hover:bg-primary/5 flex w-full items-center rounded px-2 py-1.5 text-left text-[11px]"
                              >
                                {t("team.create.manager")} · {item.name}
                              </button>
                            ))}
                        </PopoverContent>
                      </Popover>
                      <button
                        type="button"
                        aria-label={t("team.create.remove")}
                        onClick={() => toggle(employee.employee_id)}
                        className="text-chat-muted hover:text-chat-ink shrink-0"
                      >
                        <X className="size-3.5" />
                      </button>
                    </div>
                  );
                })
              )}
            </div>

            <div className="border-chat-hairline mt-2 flex h-9 shrink-0 items-center overflow-hidden rounded-lg border">
              <span className="bg-primary/[0.03] text-chat-muted flex h-full shrink-0 items-center px-2.5 text-[10px] font-medium">
                {t("team.create.nameLabel")}
              </span>
              <span className="bg-chat-hairline h-full w-px shrink-0" aria-hidden="true" />
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={t("team.create.namePlaceholder")}
                className="h-full flex-1 rounded-none border-0 bg-transparent px-2.5 text-xs shadow-none"
              />
            </div>
          </section>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("team.add.cancel")}
          </Button>
          <Button disabled={!canSubmit} onClick={() => void submit()}>
            {saving ? t("team.create.creating") : t("team.create.submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
