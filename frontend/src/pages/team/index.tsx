/**
 * 功能：团队页（员工画廊 + 员工档案抽屉 + 添加员工 + 团队管理）。
 * 数据源 = 公司域聚合服务；档案保存与员工上下线后回刷画廊。
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { AddEmployeeDialog } from "@/components/team/AddEmployeeDialog";
import { EmployeeGallery } from "@/components/team/EmployeeGallery";
import { EmployeeProfileSheet, type ProfileTab } from "@/components/team/EmployeeProfileSheet";
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
import { useT } from "@/i18n";
import type { EmployeeSummary } from "@/services/company";
import {
  describeError,
  statsFromProfile,
  teamApi,
  type EmployeeStats,
  type TeamOverview,
} from "@/services/team";

const EXAMPLE_TEMPLATE_ID = "selection-team";

export default function TeamPage() {
  const t = useT();
  const navigate = useNavigate();
  const [overview, setOverview] = useState<TeamOverview | null>(null);
  const [stats, setStats] = useState<Record<string, EmployeeStats>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reloadToken, setReloadToken] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sheetTab, setSheetTab] = useState<ProfileTab>("persona");
  const [sheetOpen, setSheetOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [retireTarget, setRetireTarget] = useState<EmployeeSummary | null>(null);

  const reload = useCallback(() => {
    setReloadToken((value) => value + 1);
    window.dispatchEvent(new Event("bandi:employees-changed"));
    window.dispatchEvent(new Event("bandi:project-teams-changed"));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    teamApi
      .overview()
      .then((data) => {
        if (cancelled) return;
        setOverview(data);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(describeError(reason, t("team.loadFailed")));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadToken, t]);

  useEffect(() => {
    const employees = overview?.employees ?? [];
    if (employees.length === 0) {
      setStats({});
      return;
    }
    let cancelled = false;
    Promise.allSettled(employees.map((employee) => teamApi.profile(employee.employee_id))).then(
      (results) => {
        if (cancelled) return;
        const next: Record<string, EmployeeStats> = {};
        employees.forEach((employee, index) => {
          const result = results[index];
          const profile = result.status === "fulfilled" ? result.value.profile : null;
          next[employee.employee_id] = statsFromProfile(profile, employee.skills ?? []);
        });
        setStats(next);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [overview]);

  const selected = overview?.employees.find((item) => item.employee_id === selectedId) ?? null;

  const openEmployee = (employee: EmployeeSummary, tab: ProfileTab = "persona") => {
    setSelectedId(employee.employee_id);
    setSheetTab(tab);
    setSheetOpen(true);
  };

  const chatWithEmployee = (employee: EmployeeSummary) => {
    navigate(`/chat?view=dm&target=${encodeURIComponent(employee.role_id)}`);
  };

  const toggleStatus = async (employee: EmployeeSummary) => {
    const next = employee.status === "active" ? "paused" : "active";
    try {
      await teamApi.updateEmployee(employee.employee_id, { status: next });
      toast.success(next === "active" ? t("team.toast.online") : t("team.toast.offline"));
      reload();
    } catch (reason) {
      toast.error(describeError(reason, t("team.toast.updateFailed")));
    }
  };

  const confirmRetire = async () => {
    const employee = retireTarget;
    if (!employee) return;
    try {
      await teamApi.purgeEmployee(employee.employee_id);
      toast.success(t("team.toast.purged"));
      setSheetOpen(false);
      setRetireTarget(null);
      reload();
    } catch (reason) {
      toast.error(describeError(reason, t("team.toast.updateFailed")));
    }
  };

  const createExampleTeam = async () => {
    try {
      await teamApi.instantiateTeam(EXAMPLE_TEMPLATE_ID, EXAMPLE_TEMPLATE_ID);
      toast.success(t("team.toast.exampleCreated"));
      reload();
    } catch (reason) {
      toast.error(describeError(reason, t("team.toast.exampleFailed")));
    }
  };

  return (
    <>
      <EmployeeGallery
        overview={overview}
        stats={stats}
        loading={loading}
        error={error}
        onAdd={() => setAddOpen(true)}
        onUseExample={() => void createExampleTeam()}
        onOpenEmployee={(employee) => openEmployee(employee)}
        onChat={chatWithEmployee}
        onChanged={reload}
        onEdit={(employee) => openEmployee(employee, "persona")}
        onToggleStatus={(employee) => void toggleStatus(employee)}
        onRetire={(employee) => setRetireTarget(employee)}
      />

      <EmployeeProfileSheet
        employee={selected}
        overview={overview}
        open={sheetOpen}
        initialTab={sheetTab}
        onOpenChange={setSheetOpen}
        onUpdated={reload}
      />

      <AddEmployeeDialog
        open={addOpen}
        overview={overview}
        onOpenChange={setAddOpen}
        onCreated={reload}
      />

      <AlertDialog
        open={retireTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRetireTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("team.purgeTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("team.purgeConfirm").replace("{name}", retireTarget?.name ?? "")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => void confirmRetire()}
            >
              {t("team.card.delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
