/**
 * 功能：把未配置团队的员工加入当前团队（创建同名岗位或把现有岗位转入团队）。
 */
import { Check, UserPlus } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

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
import type { EmployeeSummary } from "@/services/company";
import { teamApi, type TeamOverview } from "@/services/team";

export interface AddRoleToTeamDialogProps {
  open: boolean;
  teamId: string;
  overview: TeamOverview | null;
  onOpenChange: (open: boolean) => void;
  onChanged: () => void;
}

/** 功能：选择未配置团队的员工并加入当前团队。 */
export function AddRoleToTeamDialog({
  open,
  teamId,
  overview,
  onOpenChange,
  onChanged,
}: AddRoleToTeamDialogProps) {
  const t = useT();
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const roleTeamIds = useMemo(() => {
    const map = new Map<string, string>();
    (overview?.orgTeams ?? []).forEach((team) => {
      team.roles.forEach((role) => map.set(role.role_id, team.team_id));
    });
    return map;
  }, [overview?.orgTeams]);

  const candidates = useMemo<EmployeeSummary[]>(
    () =>
      (overview?.employees ?? []).filter((employee) => {
        if (employee.status === "retired") return false;
        if (!employee.role_id) return true;
        return roleTeamIds.get(employee.role_id) !== teamId;
      }),
    [overview?.employees, roleTeamIds, teamId],
  );

  const roleIds = useMemo(
    () => new Set((overview?.roles ?? []).map((role) => role.id)),
    [overview?.roles],
  );

  const toggle = (id: string) =>
    setSelectedIds((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    );

  const submit = async () => {
    if (!teamId || selectedIds.length === 0) return;
    setSaving(true);
    let failed = 0;
    for (const employeeId of selectedIds) {
      const employee = candidates.find((item) => item.employee_id === employeeId);
      if (!employee) continue;
      try {
        if (!employee.role_id) {
          const roleId = employee.employee_id;
          if (!roleIds.has(roleId)) {
            await teamApi.createRole({
              id: roleId,
              name: employee.name,
              responsibility: employee.description ?? "",
              team_id: teamId,
            });
          } else {
            await teamApi.updateRole(roleId, { team_id: teamId });
          }
          await teamApi.updateEmployee(employee.employee_id, { role_id: roleId });
        } else {
          await teamApi.updateRole(employee.role_id, { team_id: teamId });
        }
      } catch {
        failed += 1;
      }
    }
    setSaving(false);
    setSelectedIds([]);
    if (failed === 0) toast.success(t("team.addRoleDone"));
    else toast.error(`${t("team.addRoleFailed")} ${failed}`);
    onChanged();
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-sm">
            <UserPlus className="size-4" />
            {t("team.addRoleTitle")}
          </DialogTitle>
          <DialogDescription className="text-chat-muted text-[11px]">
            {t("team.addRoleHint")}
          </DialogDescription>
        </DialogHeader>

        <div className="border-chat-hairline max-h-[320px] overflow-y-auto rounded-lg border p-1">
          {candidates.length === 0 ? (
            <p className="text-chat-muted px-3 py-8 text-center text-[11px]">
              {t("team.addRoleEmpty")}
            </p>
          ) : (
            candidates.map((employee) => {
              const active = selectedIds.includes(employee.employee_id);
              return (
                <button
                  key={employee.employee_id}
                  type="button"
                  onClick={() => toggle(employee.employee_id)}
                  className="hover:bg-primary/5 flex w-full items-center gap-2 rounded px-2 py-2 text-left"
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
                  <span className="min-w-0 flex-1">
                    <span className="text-chat-ink block truncate text-xs">{employee.name}</span>
                    <span className="text-chat-muted mt-0.5 block text-[10px]">
                      {employee.role_id || t("team.addRoleNoRole")}
                    </span>
                  </span>
                </button>
              );
            })
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("team.add.cancel")}
          </Button>
          <Button disabled={saving || selectedIds.length === 0} onClick={() => void submit()}>
            {saving ? t("team.addRoleAdding") : `${t("team.addRole")} (${selectedIds.length})`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
