/**
 * 功能：员工档案「状态与组织」Tab（运行态 / 汇报链 / 所属团队 / 当前任务 / 角色工具标签）。
 */
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { EmployeeSummary, OrgTeamSummary } from "@/services/company";
import { reportsToName, teamNameOf, type OrgRoleSummary } from "@/services/team";

export interface EmployeeOrgTabProps {
  employee: EmployeeSummary;
  roles: OrgRoleSummary[];
  orgTeams: OrgTeamSummary[];
}

/** 功能：组织关系视图。 */
export function EmployeeOrgTab({ employee, roles, orgTeams }: EmployeeOrgTabProps) {
  const t = useT();
  const role = roles.find((item) => item.id === employee.role_id);
  const teamName = teamNameOf(employee, orgTeams);
  const reportsTo = reportsToName(employee, roles);
  const chain = buildChain(employee.role_id, roles);

  return (
    <div className="flex flex-col gap-4">
      <section className="grid grid-cols-2 gap-3">
        <InfoCard
          label={t("team.org.employeeStatus")}
          value={t(`team.status.${employee.status ?? "active"}`)}
        />
        <InfoCard label={t("team.org.team")} value={teamName || t("team.card.noTeam")} />
      </section>

      <section className="bg-card border-chat-hairline rounded-xl border p-4">
        <h3 className="text-chat-ink text-xs font-semibold">{t("team.org.reportingTitle")}</h3>
        {reportsTo ? (
          <p className="text-chat-muted mt-2 text-[11px]">
            {t("team.org.reportsTo")} <span className="text-chat-ink">{reportsTo}</span>
          </p>
        ) : (
          <p className="text-chat-muted mt-2 text-[11px]">{t("team.org.topLevel")}</p>
        )}
        {chain.length > 0 ? (
          <ol className="text-chat-muted mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
            {chain.map((node, index) => (
              <li key={node} className="flex items-center gap-1.5">
                {index > 0 ? <span aria-hidden="true">/</span> : null}
                <span className={cn(index === chain.length - 1 && "text-chat-ink")}>{node}</span>
              </li>
            ))}
          </ol>
        ) : null}
        <p className="text-chat-muted mt-2 text-[11px]">
          {t("team.org.responsibility")}{" "}
          {role?.responsibility?.trim() || t("team.org.noResponsibility")}
        </p>
        {role?.tool_tags?.length ? (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {role.tool_tags.map((tag) => (
              <span
                key={tag}
                className="bg-primary/[0.03] text-chat-muted rounded-md px-2 py-0.5 text-[10px]"
              >
                {tag}
              </span>
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}

function InfoCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-card border-chat-hairline rounded-xl border px-3 py-2.5">
      <p className="text-chat-muted text-[10px]">{label}</p>
      <p className="text-chat-ink mt-1 truncate text-xs font-medium">{value}</p>
    </div>
  );
}

/** 功能：沿 reports_to 向上构建汇报链（自身 → 上级 → …）。 */
function buildChain(roleId: string, roles: OrgRoleSummary[]): string[] {
  const names: string[] = [];
  const seen = new Set<string>();
  let current = roles.find((role) => role.id === roleId);
  if (current) names.push(current.name);
  while (current?.reports_to && !seen.has(current.reports_to)) {
    seen.add(current.reports_to);
    current = roles.find((role) => role.id === current?.reports_to);
    if (current) names.push(current.name);
  }
  return names;
}
