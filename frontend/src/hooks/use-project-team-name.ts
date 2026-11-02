/**
 * 功能：项目所属团队显示名（单聊页顶栏标题口径）。
 * 解析链与项目面板群聊条目一致：项目 team_ids[0] → 团队模板库名称；无模板/无团队回落空串。
 */
import { useEffect, useState } from "react";

import { companyApi } from "@/services/company";

export function useProjectTeamName(projectId: string | null | undefined): string {
  const pid = projectId ?? "";
  const [name, setName] = useState("");

  useEffect(() => {
    if (!pid) {
      setName("");
      return;
    }
    let cancelled = false;
    void Promise.all([companyApi.projects(), companyApi.teams()])
      .then(([projects, teams]) => {
        if (cancelled) return;
        const project = projects.projects.find((item) => item.project_id === pid);
        const teamId = project?.team_ids?.[0] ?? "";
        const resolved = teamId
          ? (teams.teams.find((item) => item.team_id === teamId)?.name ?? teamId)
          : "";
        setName(resolved);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [pid]);

  return name;
}
