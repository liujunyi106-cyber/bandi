/**
 * 功能：自动化页——定时任务列表、配置详情、手动触发与调度器状态（Day144）。
 */
import { ChevronLeft, Play, Plus, Search, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import {
  schedulerApi,
  type ScheduledTask,
  type ScheduleKind,
  type ScheduleSpec,
} from "@/services/scheduler";
import { settingsApi, type McpServer } from "@/services/settings";
import { useChatStore } from "@/stores/chat-store";

type FilterTab = "all" | "on" | "off" | "failed";
type PeriodUnit = "yearly" | "monthly" | "biweekly" | "weekly" | "daily";

interface FormState {
  name: string;
  project_id: string;
  prompt: string;
  target_type: string;
  target_id: string;
  enabled: boolean;
  schedule_kind: ScheduleKind;
  cron: string;
  period_unit: PeriodUnit;
  period_hour: number;
  period_minute: number;
  period_month: number;
  period_day: number;
  period_weekday: number;
  interval_hours: number;
  interval_weekdays: number[];
  once_date: string;
  once_hour: number;
  once_minute: number;
  effective_from: string;
  effective_until: string;
  mcp_servers: string[];
}

function tomorrowIso(): string {
  const date = new Date();
  date.setDate(date.getDate() + 1);
  return date.toISOString().slice(0, 10);
}

const EMPTY_FORM: FormState = {
  name: "",
  project_id: "",
  prompt: "",
  target_type: "assistant",
  target_id: "",
  enabled: true,
  schedule_kind: "periodic",
  cron: "0 9 * * *",
  period_unit: "daily",
  period_hour: 9,
  period_minute: 0,
  period_month: 1,
  period_day: 1,
  period_weekday: 1,
  interval_hours: 2,
  interval_weekdays: [1, 2, 3, 4, 5],
  once_date: tomorrowIso(),
  once_hour: 9,
  once_minute: 0,
  effective_from: "",
  effective_until: "",
  mcp_servers: [],
};

const WEEKDAYS = [
  { value: 1, label: "周一" },
  { value: 2, label: "周二" },
  { value: 3, label: "周三" },
  { value: 4, label: "周四" },
  { value: 5, label: "周五" },
  { value: 6, label: "周六" },
  { value: 7, label: "周日" },
];

const HOURS = Array.from({ length: 24 }, (_, index) => index);
const MINUTES = Array.from({ length: 60 }, (_, index) => index);
const MONTHS = Array.from({ length: 12 }, (_, index) => index + 1);
const DAYS = Array.from({ length: 31 }, (_, index) => index + 1);

const SELECT_CLASS = "border-input h-8 rounded-lg border bg-transparent px-2 text-sm outline-none";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function notifyAutomationChanged(): void {
  window.dispatchEvent(new Event("bandi:automation-changed"));
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

function weeklyLabel(weekday: number): string {
  return WEEKDAYS.find((item) => item.value === weekday)?.label ?? `周${weekday}`;
}

function scheduleText(task: ScheduledTask): string {
  const kind = task.schedule_kind || "cron";
  const spec = task.schedule_spec || {};
  if (kind === "cron") return task.cron;
  if (kind === "once") {
    return `单次 ${spec.date || ""} ${pad(spec.hour ?? 0)}:${pad(spec.minute ?? 0)}`;
  }
  if (kind === "interval") {
    return `每 ${spec.every_hours ?? 2} 小时 · ${(spec.weekdays || []).map(weeklyLabel).join(" ")}`;
  }
  const time = `${pad(spec.hour ?? 0)}:${pad(spec.minute ?? 0)}`;
  if (spec.unit === "yearly") return `每年 ${spec.month ?? 1} 月 ${spec.day ?? 1} 日 ${time}`;
  if (spec.unit === "monthly") return `每月 ${spec.day ?? 1} 日 ${time}`;
  if (spec.unit === "biweekly") return `每两周 ${weeklyLabel(spec.weekday ?? 1)} ${time}`;
  if (spec.unit === "weekly") return `每周 ${weeklyLabel(spec.weekday ?? 1)} ${time}`;
  return `每天 ${time}`;
}

function schedulePayload(form: FormState): {
  schedule_kind: ScheduleKind;
  schedule_spec: ScheduleSpec;
  cron: string;
} {
  if (form.schedule_kind === "cron") {
    return {
      schedule_kind: "cron",
      schedule_spec: { cron: form.cron.trim() },
      cron: form.cron.trim(),
    };
  }
  if (form.schedule_kind === "once") {
    return {
      schedule_kind: "once",
      schedule_spec: {
        date: form.once_date,
        hour: form.once_hour,
        minute: form.once_minute,
      },
      cron: "",
    };
  }
  if (form.schedule_kind === "interval") {
    return {
      schedule_kind: "interval",
      schedule_spec: {
        every_hours: form.interval_hours,
        weekdays: form.interval_weekdays,
      },
      cron: "",
    };
  }
  const spec: ScheduleSpec = {
    unit: form.period_unit,
    hour: form.period_hour,
    minute: form.period_minute,
  };
  if (form.period_unit === "yearly") {
    spec.month = form.period_month;
    spec.day = form.period_day;
  } else if (form.period_unit === "monthly") {
    spec.day = form.period_day;
  } else if (form.period_unit === "weekly" || form.period_unit === "biweekly") {
    spec.weekday = form.period_weekday;
  }
  return { schedule_kind: "periodic", schedule_spec: spec, cron: "" };
}

function validateSchedule(form: FormState): string | null {
  if (form.schedule_kind === "cron" && !form.cron.trim()) return "cron 表达式不能为空";
  if (form.schedule_kind === "once" && !form.once_date) return "请选择单次执行日期";
  if (form.schedule_kind === "interval" && form.interval_weekdays.length === 0) {
    return "至少选择一个执行日";
  }
  if (form.effective_from && form.effective_until && form.effective_from > form.effective_until) {
    return "生效开始日期不能晚于结束日期";
  }
  return null;
}

function formatTime(value: string | null): string {
  if (!value) return "未运行";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusOf(task: ScheduledTask): { label: string; className: string } {
  if (!task.enabled) return { label: "已停用", className: "bg-muted text-muted-foreground" };
  if (task.status === "running") return { label: "运行中", className: "bg-sky-50 text-sky-700" };
  if (task.status === "failed" || task.last_error)
    return { label: "失败", className: "bg-destructive/10 text-destructive" };
  return { label: "正常", className: "bg-emerald-50 text-emerald-700" };
}

function taskToForm(task: ScheduledTask): FormState {
  const kind = task.schedule_kind || (task.cron ? "cron" : "periodic");
  const spec = task.schedule_spec || {};
  return {
    ...EMPTY_FORM,
    name: task.name,
    project_id: task.project_id || "",
    prompt: task.prompt,
    target_type: task.target_type || "assistant",
    target_id: task.target_id || "",
    enabled: task.enabled,
    schedule_kind: kind,
    cron: task.cron || task.schedule_spec?.cron || "0 9 * * *",
    period_unit: spec.unit || "daily",
    period_hour: spec.hour ?? 9,
    period_minute: spec.minute ?? 0,
    period_month: spec.month ?? 1,
    period_day: spec.day ?? 1,
    period_weekday: spec.weekday ?? 1,
    interval_hours: spec.every_hours ?? 2,
    interval_weekdays: spec.weekdays?.length ? spec.weekdays : [1, 2, 3, 4, 5],
    once_date: spec.date || tomorrowIso(),
    once_hour: spec.hour ?? 9,
    once_minute: spec.minute ?? 0,
    effective_from: task.effective_from || "",
    effective_until: task.effective_until || "",
    mcp_servers: task.mcp_servers || [],
  };
}

export default function AutomationPage() {
  const navigate = useNavigate();
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<FilterTab>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [mcpOptions, setMcpOptions] = useState<McpServer[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const payload = await schedulerApi.list();
      setTasks(payload.tasks);
      setLoadError("");
      void schedulerApi.start().catch(() => undefined);
    } catch (error) {
      setLoadError(errorText(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void settingsApi
      .mcpServers()
      .then((payload) => setMcpOptions(payload.servers))
      .catch(() => undefined);
  }, [load]);

  const selected = useMemo(
    () => tasks.find((task) => task.task_id === selectedId) ?? null,
    [tasks, selectedId],
  );

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return tasks.filter((task) => {
      if (keyword) {
        const haystack = `${task.name} ${task.prompt} ${task.cron}`.toLowerCase();
        if (!haystack.includes(keyword)) return false;
      }
      if (tab === "on" && !task.enabled) return false;
      if (tab === "off" && task.enabled) return false;
      if (tab === "failed" && task.status !== "failed" && !task.last_error) return false;
      return true;
    });
  }, [tasks, query, tab]);

  const openCreate = () => {
    setCreating(true);
    setSelectedId(null);
    setForm(EMPTY_FORM);
  };

  const openTask = (task: ScheduledTask) => {
    setCreating(false);
    setSelectedId(task.task_id);
    setForm(taskToForm(task));
  };

  const backToList = () => {
    setCreating(false);
    setSelectedId(null);
  };

  const save = async () => {
    if (!form.name.trim() || !form.prompt.trim()) {
      toast.error("任务名称和执行内容不能为空");
      return;
    }
    const scheduleError = validateSchedule(form);
    if (scheduleError) {
      toast.error(scheduleError);
      return;
    }
    const schedule = schedulePayload(form);
    const payload = {
      name: form.name.trim(),
      project_id: form.project_id.trim() || undefined,
      prompt: form.prompt.trim(),
      target_type: form.target_type,
      target_id: form.target_type === "employee" ? form.target_id : "",
      enabled: form.enabled,
      schedule_kind: schedule.schedule_kind,
      schedule_spec: schedule.schedule_spec,
      cron: schedule.cron,
      effective_from: form.effective_from,
      effective_until: form.effective_until,
      mcp_servers: form.mcp_servers,
    };
    setSaving(true);
    try {
      if (creating) {
        const created = await schedulerApi.create(payload);
        setCreating(false);
        setSelectedId(created.task_id);
        setForm(taskToForm(created));
        toast.success("已创建任务");
      } else if (selected) {
        const updated = await schedulerApi.update(selected.task_id, payload);
        setForm(taskToForm(updated));
        toast.success("已保存");
      }
      await load();
      notifyAutomationChanged();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const trigger = async () => {
    if (!selected) return;
    setBusy(true);
    try {
      const updated = await schedulerApi.trigger(selected.task_id);
      toast.success(updated.last_output ? "运行完成" : "运行完成，暂无可见产出");
      await load();
      notifyAutomationChanged();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!selected) return;
    if (!window.confirm(`删除「${selected.name}」及其运行会话历史？`)) return;
    setBusy(true);
    try {
      await schedulerApi.remove(selected.task_id);
      toast.success("已删除任务");
      backToList();
      await load();
      notifyAutomationChanged();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (task: ScheduledTask, enabled: boolean) => {
    try {
      await schedulerApi.update(task.task_id, { enabled });
      await load();
      notifyAutomationChanged();
    } catch (error) {
      toast.error(errorText(error));
    }
  };

  const openSession = (task: ScheduledTask) => {
    if (!task.session_id) {
      toast.error("这个任务还没有运行会话");
      return;
    }
    const store = useChatStore.getState();
    store.setActiveSession(task.session_id);
    store.setActiveProject(task.project_id || "");
    navigate("/");
  };

  if (creating || selected) {
    return (
      <div className="mx-auto flex h-full min-h-0 w-full max-w-[1400px] flex-col px-8 pt-9 pb-7">
        <header className="flex shrink-0 items-center gap-3">
          <Button variant="ghost" size="sm" onClick={backToList}>
            <ChevronLeft className="size-4" />
            返回列表
          </Button>
          <h1 className="text-chat-ink min-w-0 flex-1 truncate text-xl font-semibold">
            {creating ? "新建任务" : form.name || "任务详情"}
          </h1>
          <Button variant="outline" size="sm" onClick={trigger} disabled={creating || busy}>
            <Play className="size-3.5" />
            运行测试
          </Button>
          <Button variant="outline" size="sm" onClick={remove} disabled={creating || busy}>
            <Trash2 className="size-3.5" />
            删除
          </Button>
          <Button variant="ghost" size="sm" onClick={backToList}>
            取消
          </Button>
          <Button size="sm" onClick={() => void save()} disabled={saving}>
            {saving ? "保存中…" : "保存"}
          </Button>
        </header>

        <div className="mt-4 flex min-h-0 flex-1 overflow-auto">
          <div className="border-chat-hairline bg-card grid min-h-full w-full grid-cols-1 overflow-hidden rounded-xl border lg:grid-cols-[1.55fr_1fr]">
            <section className="flex flex-col gap-5 p-6">
              <label className="flex flex-col gap-2">
                <span className="text-chat-muted text-[11px] font-medium">任务名称</span>
                <Input
                  value={form.name}
                  onChange={(event) => setForm({ ...form, name: event.target.value })}
                  placeholder="例如：每日 AI 新闻"
                />
              </label>
              <label className="flex flex-col gap-2">
                <span className="text-chat-muted text-[11px] font-medium">项目空间（可选）</span>
                <Input
                  value={form.project_id}
                  onChange={(event) => setForm({ ...form, project_id: event.target.value })}
                  placeholder="留空落左栏「自动化」；填写项目 ID 落项目面板"
                />
              </label>
              <label className="flex flex-col gap-2">
                <span className="text-chat-muted text-[11px] font-medium">执行内容</span>
                <textarea
                  value={form.prompt}
                  onChange={(event) => setForm({ ...form, prompt: event.target.value })}
                  placeholder="到点要执行什么"
                  className="border-input min-h-28 w-full rounded-lg border bg-transparent px-2.5 py-2 text-sm outline-none"
                />
              </label>
              <div className="border-chat-hairline flex flex-col gap-3 border-t pt-4">
                <span className="text-chat-muted text-[11px] font-medium">执行计划</span>
                <SegmentedTabs
                  items={[
                    { value: "periodic", label: "周期" },
                    { value: "interval", label: "按间隔" },
                    { value: "once", label: "单次" },
                    { value: "cron", label: "cron" },
                  ]}
                  value={form.schedule_kind}
                  onChange={(value) => setForm({ ...form, schedule_kind: value as ScheduleKind })}
                  ariaLabel="选择执行计划类型"
                />

                {form.schedule_kind === "periodic" ? (
                  <div className="flex flex-col gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <select
                        value={form.period_unit}
                        onChange={(event) =>
                          setForm({ ...form, period_unit: event.target.value as PeriodUnit })
                        }
                        className={SELECT_CLASS}
                        aria-label="周期单位"
                      >
                        <option value="yearly">每年</option>
                        <option value="monthly">每月</option>
                        <option value="biweekly">每两周</option>
                        <option value="weekly">每周</option>
                        <option value="daily">每天</option>
                      </select>
                      {form.period_unit === "yearly" ? (
                        <>
                          <select
                            value={form.period_month}
                            onChange={(event) =>
                              setForm({ ...form, period_month: Number(event.target.value) })
                            }
                            className={SELECT_CLASS}
                            aria-label="月份"
                          >
                            {MONTHS.map((month) => (
                              <option key={month} value={month}>
                                {month} 月
                              </option>
                            ))}
                          </select>
                          <select
                            value={form.period_day}
                            onChange={(event) =>
                              setForm({ ...form, period_day: Number(event.target.value) })
                            }
                            className={SELECT_CLASS}
                            aria-label="日期"
                          >
                            {DAYS.map((day) => (
                              <option key={day} value={day}>
                                {day} 日
                              </option>
                            ))}
                          </select>
                        </>
                      ) : null}
                      {form.period_unit === "monthly" ? (
                        <select
                          value={form.period_day}
                          onChange={(event) =>
                            setForm({ ...form, period_day: Number(event.target.value) })
                          }
                          className={SELECT_CLASS}
                          aria-label="日期"
                        >
                          {DAYS.map((day) => (
                            <option key={day} value={day}>
                              {day} 日
                            </option>
                          ))}
                        </select>
                      ) : null}
                      {form.period_unit === "weekly" || form.period_unit === "biweekly" ? (
                        <select
                          value={form.period_weekday}
                          onChange={(event) =>
                            setForm({ ...form, period_weekday: Number(event.target.value) })
                          }
                          className={SELECT_CLASS}
                          aria-label="星期"
                        >
                          {WEEKDAYS.map((weekday) => (
                            <option key={weekday.value} value={weekday.value}>
                              {weekday.label}
                            </option>
                          ))}
                        </select>
                      ) : null}
                      <select
                        value={form.period_hour}
                        onChange={(event) =>
                          setForm({ ...form, period_hour: Number(event.target.value) })
                        }
                        className={SELECT_CLASS}
                        aria-label="小时"
                      >
                        {HOURS.map((hour) => (
                          <option key={hour} value={hour}>
                            {pad(hour)} 时
                          </option>
                        ))}
                      </select>
                      <select
                        value={form.period_minute}
                        onChange={(event) =>
                          setForm({ ...form, period_minute: Number(event.target.value) })
                        }
                        className={SELECT_CLASS}
                        aria-label="分钟"
                      >
                        {MINUTES.map((minute) => (
                          <option key={minute} value={minute}>
                            {pad(minute)} 分
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                ) : null}

                {form.schedule_kind === "interval" ? (
                  <div className="flex flex-col gap-3">
                    <label className="flex items-center gap-2 text-xs">
                      每
                      <select
                        value={form.interval_hours}
                        onChange={(event) =>
                          setForm({ ...form, interval_hours: Number(event.target.value) })
                        }
                        className={SELECT_CLASS}
                        aria-label="间隔小时"
                      >
                        {Array.from({ length: 23 }, (_, index) => index + 1).map((hour) => (
                          <option key={hour} value={hour}>
                            {hour}
                          </option>
                        ))}
                      </select>
                      小时
                    </label>
                    <div className="flex flex-wrap gap-1.5">
                      {WEEKDAYS.map((weekday) => {
                        const active = form.interval_weekdays.includes(weekday.value);
                        return (
                          <button
                            key={weekday.value}
                            type="button"
                            className={`rounded-md border px-2 py-1 text-[10px] ${
                              active
                                ? "border-primary/30 bg-primary/10 text-primary"
                                : "border-chat-hairline text-chat-muted"
                            }`}
                            onClick={() =>
                              setForm({
                                ...form,
                                interval_weekdays: active
                                  ? form.interval_weekdays.filter(
                                      (value) => value !== weekday.value,
                                    )
                                  : [...form.interval_weekdays, weekday.value].sort(),
                              })
                            }
                          >
                            {weekday.label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                ) : null}

                {form.schedule_kind === "once" ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <Input
                      type="date"
                      value={form.once_date}
                      onChange={(event) => setForm({ ...form, once_date: event.target.value })}
                      className="h-8 w-[160px]"
                      aria-label="执行日期"
                    />
                    <select
                      value={form.once_hour}
                      onChange={(event) =>
                        setForm({ ...form, once_hour: Number(event.target.value) })
                      }
                      className={SELECT_CLASS}
                      aria-label="小时"
                    >
                      {HOURS.map((hour) => (
                        <option key={hour} value={hour}>
                          {pad(hour)} 时
                        </option>
                      ))}
                    </select>
                    <select
                      value={form.once_minute}
                      onChange={(event) =>
                        setForm({ ...form, once_minute: Number(event.target.value) })
                      }
                      className={SELECT_CLASS}
                      aria-label="分钟"
                    >
                      {MINUTES.map((minute) => (
                        <option key={minute} value={minute}>
                          {pad(minute)} 分
                        </option>
                      ))}
                    </select>
                  </div>
                ) : null}

                {form.schedule_kind === "cron" ? (
                  <Input
                    value={form.cron}
                    onChange={(event) => setForm({ ...form, cron: event.target.value })}
                    placeholder="30 12 * * *"
                    className="font-mono"
                  />
                ) : null}

                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="flex flex-col gap-1.5">
                    <span className="text-chat-muted text-[10px]">生效开始（可选）</span>
                    <Input
                      type="date"
                      value={form.effective_from}
                      onChange={(event) => setForm({ ...form, effective_from: event.target.value })}
                      className="h-8"
                    />
                  </label>
                  <label className="flex flex-col gap-1.5">
                    <span className="text-chat-muted text-[10px]">生效结束（可选）</span>
                    <Input
                      type="date"
                      value={form.effective_until}
                      onChange={(event) =>
                        setForm({ ...form, effective_until: event.target.value })
                      }
                      className="h-8"
                    />
                  </label>
                </div>
              </div>

              <label className="flex flex-col gap-2">
                <span className="text-chat-muted text-[11px] font-medium">执行者</span>
                <select
                  value={form.target_type === "employee" ? "employee" : "assistant"}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      target_type: event.target.value,
                      target_id: event.target.value === "employee" ? form.target_id : "",
                    })
                  }
                  className="border-input h-8 rounded-lg border bg-transparent px-2.5 text-sm outline-none"
                >
                  <option value="assistant">助理（默认）</option>
                  <option value="employee">员工（填写角色 ID）</option>
                </select>
                {form.target_type === "employee" ? (
                  <Input
                    value={form.target_id}
                    onChange={(event) => setForm({ ...form, target_id: event.target.value })}
                    placeholder="员工角色 ID"
                  />
                ) : null}
              </label>
              <div className="flex flex-col gap-2">
                <span className="text-chat-muted text-[11px] font-medium">MCP 连接器</span>
                <p className="text-chat-muted text-[10px]">
                  不选 = 继承执行者全部工具；选择后本任务只允许这些 MCP 连接器/工具。
                </p>
                {mcpOptions.length === 0 ? (
                  <p className="text-chat-muted text-[10px]">还没有配置 MCP 连接器。</p>
                ) : (
                  <div className="flex flex-col gap-2">
                    {mcpOptions.map((server) => {
                      const active = form.mcp_servers.includes(server.name);
                      const toolPrefix = `${server.prefix || server.name}_`;
                      return (
                        <div
                          key={server.name}
                          className="border-chat-hairline rounded-lg border px-2 py-1.5"
                        >
                          <button
                            type="button"
                            disabled={!server.enabled && !active}
                            className={`rounded-md border px-2 py-1 text-[10px] ${
                              active
                                ? "border-primary/30 bg-primary/10 text-primary"
                                : "border-chat-hairline text-chat-muted disabled:opacity-40"
                            }`}
                            onClick={() => {
                              const serverTools = new Set(server.tools);
                              setForm({
                                ...form,
                                mcp_servers: active
                                  ? form.mcp_servers.filter((name) => name !== server.name)
                                  : [
                                      ...form.mcp_servers.filter((name) => !serverTools.has(name)),
                                      server.name,
                                    ],
                              });
                            }}
                          >
                            {server.name}
                            <span className="ml-1 opacity-60">
                              {active ? "全部" : server.tool_count}
                            </span>
                          </button>
                          {server.tools.length > 0 ? (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {server.tools.map((tool) => {
                                const toolActive = form.mcp_servers.includes(tool);
                                return (
                                  <button
                                    key={tool}
                                    type="button"
                                    disabled={active || (!server.enabled && !toolActive)}
                                    className={`rounded px-1.5 py-0.5 text-[9px] ${
                                      toolActive
                                        ? "bg-primary/10 text-primary"
                                        : "text-chat-muted hover:text-chat-ink disabled:opacity-40"
                                    }`}
                                    onClick={() =>
                                      setForm({
                                        ...form,
                                        mcp_servers: toolActive
                                          ? form.mcp_servers.filter((name) => name !== tool)
                                          : [...form.mcp_servers, tool],
                                      })
                                    }
                                  >
                                    {tool.startsWith(toolPrefix)
                                      ? tool.slice(toolPrefix.length)
                                      : tool}
                                  </button>
                                );
                              })}
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
                />
                启用任务
              </label>
              {selected?.session_id ? (
                <Button
                  variant="outline"
                  size="sm"
                  className="w-fit"
                  onClick={() => openSession(selected)}
                >
                  在会话中打开
                </Button>
              ) : null}
            </section>

            <aside className="border-chat-hairline bg-chat-bg min-h-full border-t p-6 lg:border-t-0 lg:border-l">
              <h2 className="text-chat-ink text-sm font-semibold">运行信息</h2>
              <dl className="mt-4 space-y-3 text-xs">
                <div className="flex justify-between gap-4">
                  <dt className="text-chat-muted">状态</dt>
                  <dd>{selected ? statusOf(selected).label : "未创建"}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-chat-muted">上次运行</dt>
                  <dd>{selected ? formatTime(selected.last_run_at) : "未运行"}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-chat-muted">下次运行</dt>
                  <dd>{selected ? formatTime(selected.next_run_at) : "保存后计算"}</dd>
                </div>
                <div className="flex justify-between gap-4">
                  <dt className="text-chat-muted">会话</dt>
                  <dd className="max-w-[180px] truncate">
                    {selected?.session_id || "首次运行自动创建"}
                  </dd>
                </div>
              </dl>
              <h3 className="text-chat-ink mt-6 text-xs font-semibold">最近产出</h3>
              <pre className="text-chat-muted mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-white/70 p-3 text-[11px] leading-5">
                {selected?.last_output || selected?.last_error || "还没有运行产出。"}
              </pre>
            </aside>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-full min-h-0 w-full max-w-[1400px] flex-col px-8 pt-9 pb-7">
      <header className="shrink-0">
        <h1 className="text-chat-ink text-xl font-semibold">自动化</h1>
        <p className="text-chat-muted mt-1 text-xs">
          在会话里对助理说「每天 12:30 拉取 AI 新闻」，确认后会创建任务；到点在原会话继续执行。
        </p>
      </header>

      <div className="mt-6 flex min-h-9 shrink-0 flex-wrap items-center justify-between gap-3">
        <SegmentedTabs
          items={[
            { value: "all", label: "全部" },
            { value: "on", label: "已启用" },
            { value: "off", label: "已停用" },
            { value: "failed", label: "最近失败" },
          ]}
          value={tab}
          onChange={(value) => setTab(value as FilterTab)}
          ariaLabel="筛选定时任务"
        />
        <div className="flex items-center gap-2">
          <label className="border-chat-hairline bg-card flex h-9 w-[260px] items-center gap-2 rounded-xl border px-3">
            <Search className="text-chat-muted size-4 shrink-0" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索任务名称或执行内容"
              className="h-full min-w-0 flex-1 border-0 bg-transparent px-0 text-xs shadow-none"
            />
          </label>
          <Button onClick={openCreate}>
            <Plus />
            新建任务
          </Button>
        </div>
      </div>

      <div className="border-chat-hairline bg-card mt-4 flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border">
        {loading ? (
          <p className="text-chat-muted p-8 text-center text-xs">加载中…</p>
        ) : loadError ? (
          <div className="p-8 text-center">
            <p className="text-destructive text-xs">{loadError}</p>
            <Button variant="outline" size="sm" className="mt-3" onClick={() => void load()}>
              重试
            </Button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
            <p className="text-chat-ink text-sm font-medium">
              {tasks.length === 0 ? "还没有自动化任务" : "没有匹配的任务"}
            </p>
            <p className="text-chat-muted mt-1 text-xs">
              {tasks.length === 0 ? "在会话里让助理创建，或点右上角新建任务。" : "换个关键词试试。"}
            </p>
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-auto">
            <table className="w-full min-w-[840px] border-collapse">
              <thead className="bg-chat-bg sticky top-0 z-10">
                <tr className="text-chat-muted border-chat-hairline border-b text-left text-[11px]">
                  <th className="px-4 py-3 font-medium">任务</th>
                  <th className="px-4 py-3 font-medium">执行者</th>
                  <th className="px-4 py-3 font-medium">执行计划</th>
                  <th className="px-4 py-3 font-medium">上次运行</th>
                  <th className="px-4 py-3 font-medium">状态</th>
                  <th className="px-4 py-3 font-medium">启用</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((task) => {
                  const status = statusOf(task);
                  return (
                    <tr
                      key={task.task_id}
                      className="border-chat-hairline hover:bg-primary/[0.02] cursor-pointer border-b last:border-b-0"
                      onClick={() => openTask(task)}
                    >
                      <td className="px-4 py-3">
                        <p className="text-chat-ink truncate text-sm font-medium">{task.name}</p>
                        <p className="text-chat-muted mt-0.5 truncate text-[11px]">
                          {task.project_id || "左栏「自动化」"}
                        </p>
                      </td>
                      <td className="text-chat-muted px-4 py-3 text-xs">
                        {task.target_type === "employee" ? task.target_id || "员工" : "助理"}
                      </td>
                      <td className="px-4 py-3">
                        <p className="text-chat-ink truncate text-xs">{scheduleText(task)}</p>
                      </td>
                      <td className="text-chat-muted px-4 py-3 text-xs">
                        {formatTime(task.last_run_at)}
                      </td>
                      <td className="px-4 py-3">
                        <span
                          className={`inline-flex rounded-md px-1.5 py-0.5 text-[10px] font-medium ${status.className}`}
                        >
                          {status.label}
                        </span>
                      </td>
                      <td className="px-4 py-3" onClick={(event) => event.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={task.enabled}
                          onChange={(event) => void toggle(task, event.target.checked)}
                          aria-label={task.enabled ? "停用任务" : "启用任务"}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
