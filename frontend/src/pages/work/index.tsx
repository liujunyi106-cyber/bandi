/**
 * 功能：工作页——工作项看板（四列，卡片原地展开）。
 *
 * 数据源只有 `GET /api/company/work-items`（一次拉全量，前端按 phase 分组）：
 * 纯对话卡与历史草稿由**后端**过滤（column_counts 与 items 同源），前端只负责开关。
 *
 * 看板是执行投影，不承担规划与审批：计划由团队在群聊里自己产出，切执行模式即放行。
 * 与团队页同一套外壳（`mx-auto max-w-[1400px] px-8 pt-9`）。
 */
import { ChevronDown, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { KanbanBoard } from "@/components/work/KanbanBoard";
import type { WorkItemCardAction } from "@/components/work/WorkItemCard";
import { useT } from "@/i18n";
import { filterItems, groupByColumn } from "@/lib/work-item";
import { ApiError } from "@/services/api";
import { companyApi, type OrgTeamSummary, type WorkItemSummary } from "@/services/company";

/** 功能：轮询间隔——看板只读，10s 足够；页签隐藏时跳过。 */
const POLL_MS = 10_000;

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

export default function WorkPage() {
  const t = useT();
  const [items, setItems] = useState<WorkItemSummary[] | null>(null);
  const [error, setError] = useState("");
  const [team, setTeam] = useState("");
  const [teams, setTeams] = useState<OrgTeamSummary[]>([]);
  const [role, setRole] = useState("");
  const [query, setQuery] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [roleNames, setRoleNames] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const [result, roles, orgTeams] = await Promise.all([
        companyApi.workItems(),
        // 岗位名只用于展示，取不到就回落 id（不阻断看板）
        companyApi.roles().catch(() => ({ roles: [] })),
        companyApi.orgTeams().catch(() => ({ teams: [] })),
      ]);
      setItems(result.items ?? []);
      setTeams(orgTeams.teams ?? []);
      setRoleNames(
        Object.fromEntries((roles.roles ?? []).map((item) => [item.id, item.name])),
      );
      setError("");
    } catch (cause) {
      setError(errorText(cause));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const teamRoleIds = useMemo(() => {
    const selected = teams.find((item) => item.team_id === team);
    return new Set((selected?.roles ?? []).map((item) => item.role_id));
  }, [team, teams]);

  const roles = useMemo(() => {
    const seen = new Set<string>();
    for (const item of items ?? []) {
      if (item.assignee_role) seen.add(item.assignee_role);
    }
    return [...seen].sort();
  }, [items]);

  const visible = useMemo(
    () => filterItems(items ?? [], { query, role, teamRoleIds }),
    [items, role, query, teamRoleIds],
  );

  const counts = useMemo(() => {
    const grouped = groupByColumn(visible);
    return {
      todo: grouped.todo.length,
      in_progress: grouped.in_progress.length,
      in_review: grouped.in_review.length,
      done: grouped.done.length,
    };
  }, [visible]);

  const runAction = async (id: string, kind: WorkItemCardAction, feedback: string) => {
    setBusy(true);
    try {
      if (kind === "review-approve" || kind === "review-rework") {
        const verdict = kind === "review-approve" ? "approve" : "rework";
        await companyApi.reviewWorkItem(id, verdict, feedback);
        toast.success(t(`work.toast.${verdict}`));
      } else if (kind === "retry") {
        await companyApi.retryWorkItem(id);
        toast.success(t("work.toast.retry"));
      } else {
        await companyApi.abortWorkItem(id);
        toast.success(t("work.toast.abort"));
      }
      await load();
    } catch (cause) {
      toast.error(errorText(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto flex h-full min-h-0 w-full max-w-[1400px] flex-col gap-4 px-8 pt-9 pb-4">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-chat-ink text-xl font-semibold">{t("work.title")}</h1>
          <p className="text-chat-muted mt-1 text-xs">
            {items
              ? t("work.summary")
                  .replace("{total}", String(visible.length))
                  .replace("{todo}", String(counts.todo))
                  .replace("{progress}", String(counts.in_progress))
                  .replace("{review}", String(counts.in_review))
                  .replace("{done}", String(counts.done))
              : t("work.subtitle")}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <label className="border-chat-hairline bg-card flex h-8 w-[170px] items-center gap-1.5 rounded-lg border px-2">
            <Search className="text-chat-muted size-3.5 shrink-0" aria-hidden="true" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("work.filter.search")}
              aria-label={t("work.filter.search")}
              className="h-full min-w-0 flex-1 border-0 bg-transparent px-0 text-xs shadow-none"
            />
          </label>

          <div className="relative">
            <select
              value={team}
              onChange={(event) => setTeam(event.target.value)}
              aria-label={t("work.filter.team")}
              data-testid="work-filter-team"
              className="border-chat-hairline bg-card text-chat-ink h-8 appearance-none rounded-lg border pr-7 pl-2 text-xs"
            >
              <option value="">{t("work.filter.allTeams")}</option>
              {teams.map((item) => (
                <option key={item.team_id} value={item.team_id}>
                  {item.name}
                </option>
              ))}
            </select>
            <ChevronDown className="text-chat-muted pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2" />
          </div>

          <div className="relative">
            <select
              value={role}
              onChange={(event) => setRole(event.target.value)}
              aria-label={t("work.filter.role")}
              data-testid="work-filter-role"
              className="border-chat-hairline bg-card text-chat-ink h-8 appearance-none rounded-lg border pr-7 pl-2 text-xs"
            >
              <option value="">{t("work.filter.allRoles")}</option>
              {roles.map((name) => (
                <option key={name} value={name}>
                  {roleNames[name] ?? name}
                </option>
              ))}
            </select>
            <ChevronDown className="text-chat-muted pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2" />
          </div>

          <Button
            variant="outline"
            className="h-8 text-xs"
            data-testid="work-refresh"
            onClick={() => {
              void load();
              toast.success(t("work.toast.refreshed"));
            }}
          >
            <RefreshCw className="size-3.5" aria-hidden="true" />
            {t("work.refresh")}
          </Button>
        </div>
      </header>

      {error ? (
        <p className="rounded-lg border border-[#f3d6d3] bg-[#fdf7f5] px-2.5 py-2 text-[11.5px] text-[#9a3b1c]">
          {error}
        </p>
      ) : null}

      {items === null ? (
        <div className="text-chat-muted flex flex-1 items-center justify-center text-[11.5px]">
          {t("work.loading")}
        </div>
      ) : (
        <KanbanBoard
          items={visible}
          roleNames={roleNames}
          expandedId={expandedId}
          busy={busy}
          onToggle={(id) => setExpandedId((current) => (current === id ? null : id))}
          onAction={(id, kind, feedback) => void runAction(id, kind, feedback)}
        />
      )}
    </div>
  );
}
