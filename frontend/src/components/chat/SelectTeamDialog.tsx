/**
 * 功能：选择团队弹窗（拉入项目 = 项目群成员；选择后出现团队群聊条目）。
 * 数据源 = 已配置团队清单（org team_id 聚合；不含模板——模板实例化走团队组建入口）。
 * 拉入成功回调父组件刷新（团队群聊条目 / 员工行随之出现）。
 */
import { Users } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { companyApi, type OrgTeamSummary } from "@/services/company";

interface SelectTeamDialogProps {
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 拉入成功后回调（父组件刷新项目成员）。 */
  onAdded?: (teamName: string) => void;
}

export function SelectTeamDialog({
  projectId,
  open,
  onOpenChange,
  onAdded,
}: SelectTeamDialogProps) {
  const t = useT();
  const [teams, setTeams] = useState<OrgTeamSummary[]>([]);
  const [busyId, setBusyId] = useState("");

  useEffect(() => {
    if (!open) return;
    void companyApi
      .orgTeams()
      .then(({ teams }) => setTeams(teams))
      .catch(() => undefined);
  }, [open]);

  const add = async (team: OrgTeamSummary) => {
    setBusyId(team.team_id);
    try {
      await companyApi.addTeam(projectId, team.team_id);
      toast.success(t("chat.panel.teamAdded").replace("{name}", team.name));
      onOpenChange(false);
      onAdded?.(team.name);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : String(error));
    } finally {
      setBusyId("");
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("chat.panel.selectTeam")}</DialogTitle>
          <DialogDescription>{t("chat.panel.selectTeamHint")}</DialogDescription>
        </DialogHeader>
        <div className="max-h-72 space-y-1 overflow-y-auto">
          {teams.length === 0 ? (
            <p className="text-muted-foreground py-4 text-center text-sm">{t("common.empty")}</p>
          ) : (
            teams.map((team) => (
              <button
                key={team.team_id}
                type="button"
                data-testid="team-option"
                disabled={busyId !== ""}
                className={cn(
                  "hover:bg-primary/5 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left",
                  busyId === team.team_id && "opacity-60",
                )}
                onClick={() => void add(team)}
              >
                <Users className="text-muted-foreground size-4 shrink-0" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm">{team.name}</span>
                  <span className="text-muted-foreground block truncate text-xs">
                    {team.roles
                      .map((role) => role.name)
                      .slice(0, 4)
                      .join(" · ")}
                  </span>
                </span>
              </button>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
