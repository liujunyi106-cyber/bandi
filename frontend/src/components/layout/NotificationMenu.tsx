/**
 * 功能：消息中心（通知 + 待确认审批）——宽面板左列表右详情。
 * 入口仍在左栏底部铃铛；打开后左缘与铃铛对齐，详情不再使用内联原文。
 */
import { Bell, FileText, Trash, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { memoryApprovalApi, skillApprovalApi, type SkillPending } from "@/services/approvals";
import { ApiError } from "@/services/api";
import { teamMemoryApi, type TeamMemoryPending } from "@/services/team-memory";
import { useChatStore } from "@/stores/chat-store";

const POLL_MS = 30_000;
const BUSY_ALL = "__all__";

type MemoryPending = Awaited<ReturnType<typeof memoryApprovalApi.list>>["pending"][number];

type Filter = "all" | "team" | "long" | "draft" | "skill" | "notice";

type QueueItem =
  | { key: string; kind: "team"; record: TeamMemoryPending }
  | { key: string; kind: "memory"; item: MemoryPending; memoryKind: "long" | "draft" }
  | { key: string; kind: "skill"; item: SkillPending }
  | { key: string; kind: "notice"; item: ReturnType<typeof useChatStore.getState>["notifications"][number] };

interface ResolvedRow {
  key: string;
  action: "confirmed" | "rejected";
  at: number;
}

const RESOLVED_GRACE_MS = 8_000;

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  return new Date(seconds * 1000).toLocaleString();
}

function formatShortTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(seconds * 1000));
}

function baseName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() ?? path;
}

function firstLine(text: string): string {
  return (text || "").split("\n").find((line) => line.trim())?.trim() ?? "";
}

/** 功能：把后端判词清理成人读文案（去字段前缀/英文术语/代码围栏）。 */
function humanize(text: string): string {
  return (text || "")
    .replace(/```json[\s\S]*?```/gi, "")
    .replace(/^分析：/, "")
    .replace(/\bAgent\b/g, "员工")
    .replace(/premature conclusion/gi, "过早收口")
    .replace(/\s+/g, " ")
    .trim();
}

function reasonOf(record: TeamMemoryPending): string {
  const reason = String(record.payload?.reason ?? "").trim();
  if (reason) return reason;
  const line = (record.summary || "").split("\n").find((item) => item.startsWith("判定："));
  if (!line) return "";
  return line.match(/[（(]([^）)]+)[）)]/)?.[1]?.trim() ?? "";
}

function teamTitle(record: TeamMemoryPending): string {
  const reason = humanize(reasonOf(record));
  if (/未提供.*(误判|完成)/.test(reason)) return "信息还没给齐，就宣布完成了";
  if (/未按.*SOP.*(澄清|直接)/.test(reason)) return "没按 SOP 先澄清，就直接产出";
  return (reason || humanize(firstLine(record.summary).replace(/^任务：\s*/, ""))).slice(0, 72);
}

function teamAnalysis(record: TeamMemoryPending): string {
  const analysis = String(record.payload?.analysis ?? "").trim();
  if (analysis) return humanize(analysis);
  const lines = (record.summary || "").split("\n");
  const line =
    lines.find((item) => item.startsWith("反思：")) ??
    lines.find((item) => item.startsWith("分析："));
  return humanize((line ?? "").replace(/^反思：/, ""));
}

function teamTask(record: TeamMemoryPending): string {
  const summary = String(record.payload?.summary ?? record.summary ?? "");
  const line = summary.split("\n").find((item) => item.startsWith("任务："));
  return (line ?? "").replace(/^任务：/, "").trim();
}

function teamSkillTitle(record: TeamMemoryPending): string {
  const analysis = teamAnalysis(record);
  const reason = reasonOf(record);
  const text = `${reason}\n${analysis}`;
  if (/7\s*(个)?\s*维/.test(text)) return "7 维信息采集检查清单";
  if (reason.includes("SOP") && reason.includes("澄清")) return "按 SOP 先做需求澄清";
  const raw = String(record.payload?.suggested_skill_slug ?? "").trim();
  if (
    raw &&
    raw.length <= 24 &&
    !/^(我|你|请|给|要|帮|把|现在|先)/.test(raw) &&
    !/[吧吗呢]$/.test(raw)
  ) {
    return raw;
  }
  return "团队经验";
}

function lessonPoints(record: TeamMemoryPending): string[] {
  const text = `${reasonOf(record)}。${teamAnalysis(record)}`;
  return text
    .split(/[。\n；]/)
    .map((item) => item.trim())
    .filter((item) => item.length >= 4)
    .slice(0, 3);
}

function teamOutput(record: TeamMemoryPending): string {
  const output = String(record.payload?.output_preview ?? "").trim();
  if (output) return humanize(output);
  const line = (record.summary || "").split("\n").find((item) => item.startsWith("产出："));
  return humanize((line ?? "").replace(/^产出：/, ""));
}

function memoryKind(item: MemoryPending): "long" | "draft" {
  const kind = String(item.draft?.kind ?? "");
  if (kind === "后台直写暂存" || kind === "冲突裁决") return "draft";
  return "long";
}

function memoryTitle(item: MemoryPending): string {
  return firstLine(item.summary).replace(/^内容：\s*/, "").slice(0, 72);
}

function skillName(item: SkillPending): string {
  return String(item.name ?? item.summary ?? item.description ?? item.id).slice(0, 72);
}

function queueId(item: QueueItem): string | undefined {
  if (item.kind === "team") return item.record.id;
  if (item.kind === "notice") return item.item.id;
  return item.item.id;
}

function QueueBadge({ tone, children }: { tone: "team" | "long" | "draft" | "skill" | "notice"; children: React.ReactNode }) {
  const styles = {
    team: "border-amber-200 bg-amber-50 text-amber-700",
    long: "border-indigo-200 bg-indigo-50 text-indigo-700",
    draft: "border-slate-200 bg-slate-100 text-slate-600",
    skill: "border-teal-200 bg-teal-50 text-teal-700",
    notice: "border-border bg-muted text-muted-foreground",
  }[tone];
  return (
    <span className={cn("inline-flex shrink-0 items-center rounded-full border px-1.5 py-0.5 text-[10px] font-medium", styles)}>
      {children}
    </span>
  );
}

export function NotificationMenu() {
  const t = useT();
  const navigate = useNavigate();
  const notifications = useChatStore((state) => state.notifications);
  const unread = notifications.filter((item) => !item.read).length;
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [pending, setPending] = useState<TeamMemoryPending[]>([]);
  const [memoryPending, setMemoryPending] = useState<MemoryPending[]>([]);
  const [skillPending, setSkillPending] = useState<SkillPending[]>([]);
  const [resolved, setResolved] = useState<ResolvedRow[]>([]);
  const [selectedKey, setSelectedKey] = useState("");
  const [listExpanded, setListExpanded] = useState(false);
  const [teamError, setTeamError] = useState("");
  const [busyId, setBusyId] = useState("");

  const loadTeam = useCallback(async () => {
    try {
      const [team, memories, skills] = await Promise.allSettled([
        teamMemoryApi.list(),
        memoryApprovalApi.list(),
        skillApprovalApi.list(),
      ]);
      if (team.status === "fulfilled") setPending(team.value.pending ?? []);
      if (memories.status === "fulfilled") setMemoryPending(memories.value.pending ?? []);
      if (skills.status === "fulfilled") setSkillPending(skills.value.pending ?? []);
      const failures = [team, memories, skills].filter((item) => item.status === "rejected");
      const failure = failures[0] as PromiseRejectedResult | undefined;
      setTeamError(
        failure
          ? failure.reason instanceof Error
            ? failure.reason.message
            : String(failure.reason ?? "LOAD_FAILED")
          : "",
      );
      const now = Date.now();
      setResolved((previous) => previous.filter((item) => now - item.at < RESOLVED_GRACE_MS));
    } catch (reason) {
      setTeamError(reason instanceof Error ? reason.message : String(reason));
    }
  }, []);

  useEffect(() => {
    void loadTeam();
  }, [loadTeam]);

  useEffect(() => {
    if (!open) return;
    void loadTeam();
    const timer = window.setInterval(() => void loadTeam(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [open, loadTeam]);

  const items = useMemo<QueueItem[]>(() => {
    const result: QueueItem[] = [];
    for (const record of pending) {
      result.push({ key: `team:${record.id}`, kind: "team", record });
    }
    for (const item of memoryPending) {
      result.push({
        key: `memory:${item.id}`,
        kind: "memory",
        item,
        memoryKind: memoryKind(item),
      });
    }
    for (const item of skillPending) {
      result.push({ key: `skill:${item.id}`, kind: "skill", item });
    }
    for (const item of notifications) {
      result.push({ key: `notice:${item.id}`, kind: "notice", item });
    }
    return result;
  }, [memoryPending, notifications, pending, skillPending]);

  const filtered = useMemo(() => {
    if (filter === "all") return items;
    if (filter === "team") return items.filter((item) => item.kind === "team");
    if (filter === "long") return items.filter((item) => item.kind === "memory" && item.memoryKind === "long");
    if (filter === "draft") return items.filter((item) => item.kind === "memory" && item.memoryKind === "draft");
    if (filter === "skill") return items.filter((item) => item.kind === "skill");
    return items.filter((item) => item.kind === "notice");
  }, [filter, items]);

  const resolvedKeys = new Set(resolved.map((item) => item.key));
  const pendingVisible = filtered.filter((item) => !resolvedKeys.has(item.key));
  const visible = filter === "all" && !listExpanded ? pendingVisible.slice(0, 5) : pendingVisible;

  useEffect(() => {
    if (!open) return;
    if (!filtered.some((item) => item.key === selectedKey)) {
      setSelectedKey(filtered[0]?.key ?? "");
    }
  }, [filtered, open, selectedKey]);

  const selected = items.find((item) => item.key === selectedKey) ?? null;
  const [skillDraft, setSkillDraft] = useState("");
  const [createSkill, setCreateSkill] = useState(true);
  useEffect(() => {
    if (selected?.kind === "team") {
      setSkillDraft(teamSkillTitle(selected.record));
      setCreateSkill(true);
    }
  }, [selectedKey]);
  const pendingCount = pending.length + memoryPending.length + skillPending.length;
  const badge = unread + pendingCount;

  const markResolved = (key: string, action: "confirmed" | "rejected") => {
    setResolved((previous) => [
      ...previous.filter((item) => item.key !== key),
      { key, action, at: Date.now() },
    ]);
    setSelectedKey("");
  };

  const runTeam = async (record: TeamMemoryPending, action: "confirmed" | "rejected") => {
    setBusyId(record.id);
    try {
      if (action === "confirmed") {
        await teamMemoryApi.confirm(record.id, {
          create_skill: createSkill,
          skill_name: skillDraft.trim(),
        });
      } else {
        await teamMemoryApi.reject(record.id);
      }
      markResolved(`team:${record.id}`, action);
      await loadTeam();
    } catch (error) {
      setTeamError(error instanceof ApiError ? error.message : String(error));
    } finally {
      setBusyId("");
    }
  };

  const runMemory = async (item: MemoryPending, approve: boolean) => {
    setBusyId(item.id);
    try {
      if (approve) await memoryApprovalApi.approve(item.id);
      else await memoryApprovalApi.reject(item.id);
      markResolved(`memory:${item.id}`, approve ? "confirmed" : "rejected");
      await loadTeam();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setTeamError(message);
      toast.error(message);
    } finally {
      setBusyId("");
    }
  };

  const runSkill = async (item: SkillPending, approve: boolean) => {
    setBusyId(item.id);
    try {
      if (approve) await skillApprovalApi.approve(item.id);
      else await skillApprovalApi.reject(item.id);
      markResolved(`skill:${item.id}`, approve ? "confirmed" : "rejected");
      await loadTeam();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setTeamError(message);
      toast.error(message);
    } finally {
      setBusyId("");
    }
  };

  const rejectAll = async () => {
    setBusyId(BUSY_ALL);
    try {
      const results = await Promise.allSettled([
        ...pending.map((item) => teamMemoryApi.reject(item.id)),
        ...memoryPending.map((item) => memoryApprovalApi.reject(item.id)),
        ...skillPending.map((item) => skillApprovalApi.reject(item.id)),
      ]);
      const failed = results.filter((item) => item.status === "rejected").length;
      if (failed > 0) toast.error(t("notify.rejectAllFailed").replace("{n}", String(failed)));
      else toast.success(t("notify.rejectAllDone").replace("{n}", String(results.length)));
      if (notifications.length > 0) useChatStore.getState().clearNotifications();
      setSelectedKey("");
      await loadTeam();
    } finally {
      setBusyId("");
    }
  };

  const changeFilter = (next: Filter) => {
    setFilter(next);
    setListExpanded(false);
    const first = items.find((item) => {
      if (next === "all") return true;
      if (next === "team") return item.kind === "team";
      if (next === "long") return item.kind === "memory" && item.memoryKind === "long";
      if (next === "draft") return item.kind === "memory" && item.memoryKind === "draft";
      if (next === "skill") return item.kind === "skill";
      return item.kind === "notice";
    });
    setSelectedKey(first?.key ?? "");
  };

  const renderTeamDetail = (record: TeamMemoryPending) => {
    const employee = String(record.payload?.employee_id ?? "");
    const team = String(record.payload?.team_id ?? "");
    const project = String(record.payload?.project_id ?? "");
    const employeeName = record.employee_name || employee || "未指派";
    const teamName = record.team_name || team;
    const projectName = record.project_name || project;
    const analysis = teamAnalysis(record);
    const output = teamOutput(record);
    const reason = reasonOf(record);
    const task = teamTask(record);
    const points = lessonPoints(record);
    const advice = record.payload?.escalation as
      | { opinion?: string; reporter_role?: string }
      | undefined;
    const adviceText = [advice?.reporter_role, advice?.opinion].filter(Boolean).join(" · ");
    const files = Array.isArray(record.payload?.files)
      ? record.payload.files.map(String).filter(Boolean)
      : [];
    return (
      <div data-testid="team-memory-card" className="space-y-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <QueueBadge tone="team">返工教训</QueueBadge>
          <span className="text-[10px] text-muted-foreground">
            {employeeName}
            {teamName ? ` · ${teamName}` : ""}
          </span>
        </div>
        <h3 className="text-[17px] font-semibold leading-snug">{teamTitle(record)}</h3>
        <p className="text-[10px] text-muted-foreground">
          {formatShortTime(record.created_at)}
          {projectName ? ` · ${projectName}` : ""}
        </p>
        <section>
          <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">
            {t("teamMemory.field.whatHappened")}
          </h4>
          <div className="rounded-xl border border-border/70 bg-muted/40 px-3 py-2.5">
            {task ? (
              <p className="text-[11px] leading-relaxed">
                <span className="text-muted-foreground">你说：</span>
                {task}
              </p>
            ) : null}
            <p className="mt-1.5 text-[11px] leading-relaxed whitespace-pre-wrap">
              {analysis || firstLine(record.summary)}
            </p>
          </div>
          {output ? (
            <p className="text-muted-foreground mt-1.5 text-[10px] leading-relaxed">
              产出：{output}
            </p>
          ) : null}
        </section>
        <section>
          <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">
            {t("teamMemory.field.lesson")}
          </h4>
          <ul className="space-y-1.5">
            {(points.length ? points : [reason || t("teamMemory.field.legacyHint")]).map((point) => (
              <li key={point} className="flex gap-1.5 text-[11px] leading-relaxed">
                <span className="mt-[3px] size-1.5 shrink-0 rounded-full bg-teal-500" />
                <span>{point}</span>
              </li>
            ))}
          </ul>
        </section>
        <section className="rounded-xl border border-teal-100 bg-teal-50/50 px-3 py-2.5">
          <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">
            {t("teamMemory.field.willWrite")}
          </h4>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[10px] text-muted-foreground">
                {t("teamMemory.field.experience")}
              </p>
              <p className="mt-0.5 truncate text-[12px] font-medium">
                {skillDraft || teamSkillTitle(record)}
              </p>
            </div>
            <div className="flex shrink-0 flex-wrap justify-end gap-1">
              <span className="rounded-md bg-white/70 px-1.5 py-0.5 text-[10px]">
                {employeeName}
              </span>
              {projectName ? (
                <span className="rounded-md bg-white/70 px-1.5 py-0.5 text-[10px]">
                  {projectName}
                </span>
              ) : null}
            </div>
          </div>
          <div className="mt-2 flex items-start gap-2 border-t border-teal-100 pt-2">
            <button
              type="button"
              role="switch"
              aria-checked={createSkill}
              aria-label={t("teamMemory.field.skillToggle")}
              onClick={() => setCreateSkill((value) => !value)}
              className={cn(
                "relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors",
                createSkill ? "bg-primary" : "bg-muted-foreground/25",
              )}
            >
              <span
                className={cn(
                  "absolute top-0.5 size-4 rounded-full bg-white transition-transform",
                  createSkill ? "left-[18px]" : "left-0.5",
                )}
              />
            </button>
            <div className="min-w-0 flex-1">
              <p className="text-[11px]">{t("teamMemory.field.skillToggle")}</p>
              <p className="text-[10px] text-muted-foreground">
                {t("teamMemory.field.skillToggleHint")}
              </p>
            </div>
          </div>
          {createSkill ? (
            <input
              value={skillDraft}
              onChange={(event) => setSkillDraft(event.target.value)}
              aria-label={t("teamMemory.field.skillName")}
              className="mt-2 w-full rounded-lg border border-teal-100 bg-white px-2.5 py-1.5 text-[11px] outline-none focus:border-teal-300"
            />
          ) : null}
        </section>
        {files.length > 0 ? (
          <details className="rounded-xl border border-border/70 px-3 py-2">
            <summary className="text-muted-foreground cursor-pointer text-[10px]">
              {t("teamMemory.field.evidence")} · {files.length}
            </summary>
            <ul className="mt-1.5 space-y-1">
              {files.map((file) => (
                <li key={file} className="flex items-center gap-1.5 text-[10px]" title={file}>
                  <FileText className="text-muted-foreground size-3 shrink-0" />
                  <span className="truncate">{baseName(file)}</span>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
        <p className="text-muted-foreground text-[10px]">
          {t("teamMemory.source")}: {[employeeName, projectName, teamName].filter(Boolean).join(" · ")}
        </p>
        {adviceText ? (
          <p
            data-testid="team-memory-advice"
            className="rounded-lg border border-border/70 px-2.5 py-1.5 text-[10px] text-muted-foreground"
          >
            <span className="font-medium">{t("teamMemory.adviceOnly")}</span>：{adviceText}
          </p>
        ) : null}
      </div>
    );
  };

  const renderMemoryDetail = (item: MemoryPending) => {
    const isDraft = memoryKind(item) === "draft";
    const target = String(item.draft?.target ?? (isDraft ? "长期记忆" : "长期记忆"));
    const origin = item.origin === "user_like" ? t("notify.source.userLike") : t("notify.source.auto");
    const lines = (item.summary || "").split("\n").filter((line) => line.trim());
    return (
      <div data-testid="approval-detail-personal" className="space-y-4">
        <div className="flex flex-wrap items-center gap-1.5">
          <QueueBadge tone={isDraft ? "draft" : "long"}>
            {isDraft ? t("notify.kind.draft") : t("notify.kind.nominated")}
          </QueueBadge>
          {item.conflict ? (
            <span className="rounded-full border border-red-200 bg-red-50 px-1.5 py-0.5 text-[10px] text-red-700">
              {t("notify.conflictBadge")}
            </span>
          ) : null}
        </div>
        <section>
          <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">
            {isDraft ? "草稿内容" : "记忆内容"}
          </h4>
          <p className="rounded-xl border border-border/70 bg-muted/40 px-3 py-2.5 text-[11px] leading-relaxed whitespace-pre-wrap">
            {lines.join("\n")}
          </p>
        </section>
        <section className="rounded-xl border border-border/70 px-3 py-2.5">
          <p className="text-[11px]">
            <span className="text-muted-foreground">写入目标：</span>
            {target}
          </p>
          <p className="mt-1 text-[11px]">
            <span className="text-muted-foreground">来源：</span>
            {origin}
          </p>
          {isDraft ? (
            <p className="mt-1 text-[11px] text-muted-foreground">
              批准后草稿转为正式记忆，才会进入召回。
            </p>
          ) : null}
        </section>
        {item.conflict && item.old_value ? (
          <section>
            <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">与旧记忆对照</h4>
            <div className="rounded-xl border border-red-100 bg-red-50/40 px-3 py-2 text-[11px]">
              <p className="text-muted-foreground">旧值</p>
              <p className="mt-1 whitespace-pre-wrap">{item.old_value}</p>
            </div>
          </section>
        ) : null}
      </div>
    );
  };

  const renderSkillDetail = (item: SkillPending) => (
    <div data-testid="approval-detail-skill" className="space-y-4">
      <QueueBadge tone="skill">{t("notify.kind.skill")}</QueueBadge>
      <section>
        <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">技能卡名称</h4>
        <p className="rounded-xl border border-border/70 bg-muted/40 px-3 py-2.5 text-[12px] font-medium">
          {skillName(item)}
        </p>
        <p className="text-muted-foreground mt-1.5 text-[10px]">
          批准后生成技能卡并写入员工档案，供后续任务复用。
        </p>
      </section>
      {item.employee_id ? (
        <p className="text-[11px]">
          <span className="text-muted-foreground">来源员工：</span>
          {item.employee_id}
        </p>
      ) : null}
      {item.summary ? (
        <section>
          <h4 className="text-muted-foreground mb-1.5 text-[10px] font-medium">摘要</h4>
          <p className="text-[11px] leading-relaxed whitespace-pre-wrap">{item.summary}</p>
        </section>
      ) : null}
    </div>
  );

  const renderActions = () => {
    if (!selected || selected.kind === "notice") return null;
    if (selected.kind === "team") {
      return (
        <>
          <Button
            variant="outline"
            size="sm"
            disabled={busyId === selected.record.id}
            data-testid="team-memory-reject"
            onClick={() => void runTeam(selected.record, "rejected")}
          >
            {t("teamMemory.reject")}
          </Button>
          <Button
            size="sm"
            disabled={busyId === selected.record.id}
            data-testid="team-memory-approve"
            onClick={() => void runTeam(selected.record, "confirmed")}
          >
            {t("teamMemory.confirm")}
          </Button>
        </>
      );
    }
    if (selected.kind === "memory") {
      const draft = memoryKind(selected.item) === "draft";
      return (
        <>
          <Button
            variant="outline"
            size="sm"
            disabled={busyId === selected.item.id}
            data-testid="approval-reject-personal"
            onClick={() => void runMemory(selected.item, false)}
          >
            {t("notify.reject")}
          </Button>
          <Button
            size="sm"
            disabled={busyId === selected.item.id}
            data-testid="approval-approve-personal"
            onClick={() => void runMemory(selected.item, true)}
          >
            {draft ? "批准转正" : "批准写入"}
          </Button>
        </>
      );
    }
    return (
      <>
        <Button
          variant="outline"
          size="sm"
          disabled={busyId === selected.item.id}
          data-testid="approval-reject-skill"
          onClick={() => void runSkill(selected.item, false)}
        >
          {t("notify.reject")}
        </Button>
        <Button
          size="sm"
          disabled={busyId === selected.item.id}
          data-testid="approval-approve-skill"
          onClick={() => void runSkill(selected.item, true)}
        >
          批准生成技能卡
        </Button>
      </>
    );
  };

  const renderDetail = () => {
    if (!selected) {
      return (
        <div className="flex h-full items-center justify-center px-8 text-center text-xs text-muted-foreground">
          选择左侧一条记录查看详情
        </div>
      );
    }
    if (selected.kind === "team") return renderTeamDetail(selected.record);
    if (selected.kind === "memory") return renderMemoryDetail(selected.item);
    if (selected.kind === "skill") return renderSkillDetail(selected.item);
    return (
      <div className="space-y-4">
        <QueueBadge tone="notice">通知</QueueBadge>
        <h3 className="text-base font-semibold">{selected.item.title}</h3>
        {selected.item.body ? (
          <p className="text-muted-foreground text-[11px] leading-relaxed whitespace-pre-wrap">
            {selected.item.body}
          </p>
        ) : null}
        {selected.item.sessionId ? (
          <Button
            size="sm"
            onClick={() => {
              useChatStore.getState().setActiveSession(selected.item.sessionId!);
              setOpen(false);
              navigate("/");
            }}
          >
            打开会话
          </Button>
        ) : null}
      </div>
    );
  };

  const filters: { key: Filter; label: string }[] = [
    { key: "all", label: "全部" },
    { key: "team", label: "团队记忆" },
    { key: "long", label: "长期记忆" },
    { key: "draft", label: "草稿记忆" },
    { key: "skill", label: "技能提名" },
    { key: "notice", label: "通知" },
  ];

  return (
    <Popover open={open} onOpenChange={(next) => {
      setOpen(next);
      if (next) useChatStore.getState().markNotificationsRead();
    }}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          title={t("header.notifications")}
          className="no-focus-ring relative"
          data-testid="notification-trigger"
        >
          <Bell strokeWidth={1.75} />
          {badge > 0 ? (
            <span className="bg-destructive text-destructive-foreground absolute -top-0.5 -right-0.5 flex size-4 items-center justify-center rounded-full text-[9px] font-medium">
              {badge > 9 ? "9+" : badge}
            </span>
          ) : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        sideOffset={8}
        role="region"
        aria-label={t("header.notifications")}
        className="flex h-[min(660px,calc(100vh-90px))] w-[min(740px,calc(100vw-24px))] flex-col gap-0 overflow-hidden rounded-2xl border border-border bg-card p-0 shadow-xl"
      >
        <header className="flex items-center justify-between px-4 py-3">
          <h2 className="text-[15px] font-semibold">消息中心</h2>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="关闭"
            data-testid="notification-close"
            onClick={() => setOpen(false)}
          >
            <X className="size-3.5" />
          </Button>
        </header>

        <div
          className="flex items-center gap-2 border-b border-border px-4 py-2"
          data-testid="notification-filter-row"
        >
          <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
            {filters.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => changeFilter(item.key)}
                className={cn(
                  "shrink-0 rounded-full border border-transparent px-2.5 py-1 text-[10.5px] text-muted-foreground transition-colors",
                  filter === item.key && "border-transparent bg-foreground font-medium text-background",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>
          <Button
            variant="ghost"
            size="icon-xs"
            className="text-muted-foreground hover:text-foreground shrink-0"
            title={t("header.clearNotifications")}
            data-testid="clear-notifications"
            disabled={busyId === BUSY_ALL || (notifications.length === 0 && pendingCount === 0)}
            onClick={() => void rejectAll()}
          >
            <Trash className="size-3.5" />
          </Button>
        </div>

        {teamError ? (
          <p className="border-b border-border px-4 py-2 text-[11px] text-destructive" role="alert">
            {teamError}
          </p>
        ) : null}

        <div className="flex min-h-0 flex-1">
          <aside
            className="w-[218px] shrink-0 border-r border-border p-1.5"
            data-testid={pending.length > 0 ? "team-memory-list" : undefined}
          >
            <ScrollArea className="h-full">
            {filtered.length === 0 ? (
              <p className="px-2 py-6 text-center text-[11px] text-muted-foreground">
                {filter === "all" ? "暂无通知" : "暂无内容"}
              </p>
            ) : null}
            {visible.map((item) => {
              const isResolved = resolvedKeys.has(item.key);
              const title =
                item.kind === "team"
                  ? teamTitle(item.record)
                  : item.kind === "memory"
                    ? memoryTitle(item.item)
                    : item.kind === "skill"
                      ? skillName(item.item)
                      : item.item.title;
              const meta =
                item.kind === "team"
                  ? [
                      [
                        item.record.employee_name || item.record.payload?.employee_id,
                        item.record.team_name || item.record.payload?.team_id,
                      ]
                        .filter(Boolean)
                        .join(" · "),
                      formatShortTime(item.record.created_at),
                    ]
                  : item.kind === "memory"
                    ? [item.memoryKind === "draft" ? "后台直写暂存" : "长期记忆", formatTime(item.item.created_at ?? 0)]
                    : item.kind === "skill"
                      ? [item.item.employee_id ?? "技能提名", formatTime(item.item.created_at ?? 0)]
                      : [formatTime(item.item.ts), ""];
              const tone =
                item.kind === "team"
                  ? "team"
                  : item.kind === "memory"
                    ? item.memoryKind
                    : item.kind === "skill"
                      ? "skill"
                      : "notice";
              const testId =
                item.kind === "team"
                  ? "team-memory-toggle"
                  : item.kind === "memory"
                    ? "approval-row-personal"
                    : item.kind === "skill"
                      ? "approval-row-skill"
                      : "notification-row";
              return (
                <button
                  key={item.key}
                  type="button"
                  data-testid={testId}
                  data-approval-id={item.kind === "notice" ? undefined : queueId(item)}
                  aria-current={selectedKey === item.key}
                  onClick={() => setSelectedKey(item.key)}
                  className={cn(
                    "mb-1 block w-full rounded-xl px-2.5 py-2 text-left transition-colors hover:bg-muted/60",
                    selectedKey === item.key && "bg-muted",
                    isResolved && "opacity-50",
                  )}
                >
                  <div className="flex items-center gap-1.5">
                    <QueueBadge tone={tone}>
                      {item.kind === "team"
                        ? "返工"
                        : item.kind === "memory"
                          ? item.memoryKind === "draft"
                            ? "草稿记忆"
                            : "长期记忆"
                          : item.kind === "skill"
                            ? "技能提名"
                            : "通知"}
                    </QueueBadge>
                    {isResolved ? (
                      <span className="text-[10px] text-muted-foreground">
                        {resolved.find((row) => row.key === item.key)?.action === "confirmed" ? "已批准" : "已驳回"}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1 line-clamp-2 text-[11.5px] font-medium leading-snug">
                    {title}
                  </div>
                  <div className="mt-1 truncate text-[10px] text-muted-foreground">
                    {meta.filter(Boolean).join(" · ")}
                  </div>
                </button>
              );
            })}
            {filter === "all" && filtered.length > 5 ? (
              <button
                type="button"
                data-testid="approval-show-all"
                className="flex w-full items-center gap-0.5 px-2 py-1.5 text-[10px] text-muted-foreground hover:text-foreground"
                onClick={() => setListExpanded((value) => !value)}
              >
                {listExpanded ? "收起" : `查看更多（${filtered.length - 5}）`}
              </button>
            ) : null}
            {resolved.map((item) => (
              <p
                key={item.key}
                data-testid="team-memory-resolved"
                className="px-2 py-1 text-[10px] text-muted-foreground"
              >
                {item.action === "confirmed"
                  ? item.key.startsWith("team:")
                    ? "已批准进团队记忆"
                    : "已批准"
                  : "已驳回"}
              </p>
            ))}
            </ScrollArea>
          </aside>

          <section className="min-w-0 flex-1">
            <ScrollArea className="h-full">
              <div className="px-5 py-4">{renderDetail()}</div>
            </ScrollArea>
          </section>
        </div>
        {selected && selected.kind !== "notice" ? (
          <footer className="flex items-center justify-between gap-3 border-t border-border px-4 py-3">
            <p className="text-muted-foreground text-[10px] leading-relaxed">
              批准后按类型写入团队记忆、长期记忆、草稿转正或技能库；不会自动修改员工的人设与权限。
            </p>
            <div className="flex shrink-0 items-center gap-2">{renderActions()}</div>
          </footer>
        ) : null}
      </PopoverContent>
    </Popover>
  );
}
