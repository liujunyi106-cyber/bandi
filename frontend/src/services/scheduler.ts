/**
 * 功能：定时任务 API——列表/详情/创建/更新/删除/触发/调度器状态。
 */
import { api } from "@/services/api";

export type ScheduleKind = "cron" | "periodic" | "interval" | "once";

export interface ScheduleSpec {
  unit?: "yearly" | "monthly" | "biweekly" | "weekly" | "daily";
  hour?: number;
  minute?: number;
  month?: number;
  day?: number;
  weekday?: number;
  every_hours?: number;
  weekdays?: number[];
  date?: string;
  cron?: string;
}

export interface ScheduledTask {
  task_id: string;
  name: string;
  cron: string;
  prompt: string;
  enabled: boolean;
  status: "idle" | "running" | "failed";
  next_run_at: string | null;
  last_run_at: string | null;
  last_error: string | null;
  last_output: string;
  target_type: string;
  target_id: string;
  description: string;
  session_id: string;
  project_id: string;
  employee_id: string;
  schedule_kind: ScheduleKind;
  schedule_spec: ScheduleSpec;
  effective_from: string;
  effective_until: string;
  mcp_servers: string[];
}

export interface SchedulerStatus {
  running: boolean;
  tasks: number;
}

export interface ScheduledTaskInput {
  name: string;
  cron?: string;
  prompt: string;
  enabled?: boolean;
  target_type?: string;
  target_id?: string;
  description?: string;
  session_id?: string;
  project_id?: string;
  employee_id?: string;
  schedule_kind?: ScheduleKind;
  schedule_spec?: ScheduleSpec;
  effective_from?: string;
  effective_until?: string;
  mcp_servers?: string[];
}

export const schedulerApi = {
  list: () => api.get<{ tasks: ScheduledTask[] }>("/scheduler/tasks"),
  get: (taskId: string) => api.get<ScheduledTask>(`/scheduler/tasks/${taskId}`),
  create: (body: ScheduledTaskInput) => api.post<ScheduledTask>("/scheduler/tasks", body),
  update: (taskId: string, body: Partial<ScheduledTaskInput>) =>
    api.patch<ScheduledTask>(`/scheduler/tasks/${taskId}`, body),
  remove: (taskId: string) => api.del<{ deleted: string }>(`/scheduler/tasks/${taskId}`),
  trigger: (taskId: string) => api.post<ScheduledTask>(`/scheduler/tasks/${taskId}/trigger`),
  status: () => api.get<SchedulerStatus>("/scheduler/status"),
  start: () => api.post<{ running: boolean }>("/scheduler/start"),
  stop: () => api.post<{ running: boolean }>("/scheduler/stop"),
};
