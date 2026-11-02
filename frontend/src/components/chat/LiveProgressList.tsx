/**
 * 功能：员工**进行中**过程块的实时列表（会话视图消息流末尾）。
 *
 * 解决的问题：过程块此前只在「员工回复落地」那一刻被固化消费，执行期间槽里已有
 * 思考/工具/skills 节点却无任何视图渲染 ⇒ 用户全程只看到占位信息。多员工协作时
 * 只有最后落消息的那个员工的过程被固化，其余并发员工的中间过程完全丢失。
 *
 * 语义：固化（`consumeEmployeeProgress`）会清空槽 ⇒ 本列表随之消失，不会与固化块重复。
 * 同一员工多次执行按线程分槽，各自独立成条。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { ProcessBlock } from "@/components/chat/ProcessBlock";
import { APPROVAL_FRAME_PREFIX } from "@/hooks/use-company-approvals";
import { useEmployees } from "@/hooks/use-employees";
import { useT } from "@/i18n";
import { employeeTone, resolveEmployeeName } from "@/lib/employee";
import { cn } from "@/lib/utils";
import { companyApi, type WorkItemSummary } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";
import { toneClass } from "@/theme";

import { useLiveEmployeeProgress } from "@/hooks/use-live-employee-progress";
import { normalizeProjectScope } from "@/lib/progress-key";
import { socket } from "@/services/ws";

/** 功能：回复已落位后，早于回复创建的 running 卡不再作为 fallback 复活。 */
export function fallbackRoleVisible(item: WorkItemSummary, finishedAt?: number): boolean {
  if (!finishedAt || !item.created_at) return true;
  const created = Date.parse(item.created_at.replace(" ", "T"));
  return !(Number.isFinite(created) && created <= finishedAt);
}

export function LiveProgressList({
  projectId,
  threadId,
  className,
}: {
  projectId?: string | null;
  threadId?: string | null;
  className?: string;
}) {
  const { employees } = useEmployees();
  const live = useLiveEmployeeProgress(projectId, threadId);
  const progressTexts = useChatStore((state) => state.employeeProgressText);
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const interactions = useChatStore((state) => state.interactions);
  const t = useT();
  // running 卡占位：刷新/断线后过程帧丢失时段——按项目内 running 工作项
  // 补齐「员工正在执行」呈现，不再空窗；帧到达后由真实过程块接管
  const [runningRoles, setRunningRoles] = useState<string[]>([]);
  const finishedRolesRef = useRef<Record<string, number>>({});
  useEffect(() => {
    if (!projectId) {
      setRunningRoles([]);
      return;
    }
    let cancelled = false;
    const load = () =>
      companyApi
        .workItems()
        .then((result) => {
          if (cancelled) return;
          const roles = Array.from(
            new Set(
              result.items
                .filter(
                  (item) =>
                    item.phase === "running" &&
                    item.project_id === projectId &&
                    Boolean(item.assignee_role) &&
                    fallbackRoleVisible(item, finishedRolesRef.current[String(item.assignee_role)]),
                )
                .map((item) => String(item.assignee_role)),
            ),
          );
          setRunningRoles(roles);
        })
        .catch(() => undefined);
    void load();
    const timer = window.setInterval(load, 5_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [projectId]);
  // 员工挂起于多问卡/权限卡（等用户决策）——过程块据此显示「等待你的决策…」，
  // 不再让用户误以为系统卡死；答复后（卡消失）恢复「正在执行…」。
  const waitingForUser = useMemo(
    () =>
      Object.values(interactions).some(
        (frame) =>
          frame.session_id === activeSessionId &&
          !String(frame.interaction_id).startsWith(APPROVAL_FRAME_PREFIX),
      ),
    [interactions, activeSessionId],
  );
  // 功能：员工回复落位 → 立即移除该员工的「正在执行」占位（不等 20s 轮询）
  useEffect(() => {
    if (!projectId) return;
    return socket.on("thread_message", (frame) => {
      const data =
        frame.data && typeof frame.data === "object" ? (frame.data as Record<string, unknown>) : {};
      if (normalizeProjectScope(String(data.project_id ?? "")) !== normalizeProjectScope(projectId))
        return;
      const sender = String(data.sender ?? "").trim();
      if (!sender) return;
      finishedRolesRef.current[sender] = Date.now();
      setRunningRoles((prev) => prev.filter((role) => role !== sender));
    });
  }, [projectId]);
  useEffect(() => {
    if (!projectId) return;
    return socket.on("thread_progress", (frame) => {
      const data =
        frame.data && typeof frame.data === "object" ? (frame.data as Record<string, unknown>) : {};
      if (normalizeProjectScope(String(data.project_id ?? "")) !== normalizeProjectScope(projectId))
        return;
      const event =
        data.event && typeof data.event === "object"
          ? (data.event as Record<string, unknown>)
          : {};
      if (String(event.type ?? "") !== "started") return;
      const roleId = String(data.role_id ?? "").trim();
      if (roleId) delete finishedRolesRef.current[roleId];
    });
  }, [projectId]);
  // 功能：员工过程已出现（头像/名字/执行中）→ 立即清掉「已转交员工」过渡注记
  useEffect(() => {
    if (!activeSessionId) return;
    if (live.length === 0 && runningRoles.length === 0) return;
    useChatStore.getState().clearTransientNotes(activeSessionId);
  }, [activeSessionId, live.length, runningRoles.length]);
  // 回复已落位的员工即使轮询状态尚未收敛，也不得再以 fallback 占位复活。
  const visibleRunningRoles = runningRoles.filter(
    (roleId) => !finishedRolesRef.current[roleId],
  );
  if (live.length === 0 && visibleRunningRoles.length === 0) return null;
  // 全部活跃员工过程均渲染（并行/连续调度时需要看到其他员工的过程块与身份头；
  // 串行时天然只有一个活跃）
  const visible = live;

  return (
    <div
      data-testid="live-employee-progress"
      className={cn("flex w-full flex-col gap-3", className)}
    >
      {live.length === 0
        ? visibleRunningRoles.map((roleId) => {
            const name = resolveEmployeeName(employees, roleId);
            return (
              <div
                key={`pending::${roleId}`}
                data-testid="live-employee-progress-item"
                data-role-id={roleId}
                className="flex w-full items-center gap-2"
              >
                <span
                  className={cn(
                    "flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-medium",
                    toneClass(employeeTone(roleId)),
                  )}
                >
                  {name.slice(0, 1)}
                </span>
                <span className="text-chat-ink text-sm">{name}</span>
                <span className="bg-primary/60 size-1.5 animate-pulse rounded-full" />
                <span className="text-chat-muted text-xs">{t("chat.dm.running")}</span>
              </div>
            );
          })
        : visible.map((item) => {
        const name = resolveEmployeeName(employees, item.roleId);
        return (
          <div
            key={`${item.threadId}::${item.roleId}`}
            data-testid="live-employee-progress-item"
            data-role-id={item.roleId}
            data-thread-id={item.threadId}
            className="flex w-full flex-col items-start"
          >
            {/* 员工身份头与固化后的员工消息行同口径（头像 size-6/text-xs 对齐
                MessageItem） */}
            <span className="text-chat-muted mb-1 flex items-center gap-2 text-sm">
              <span
                className={cn(
                  "flex size-6 items-center justify-center rounded-full text-xs font-medium",
                  toneClass(employeeTone(item.roleId)),
                )}
              >
                {name.slice(0, 1)}
              </span>
              {name}
            </span>
            {/* 过程块与头像左侧对齐（不缩进到姓名右缘） */}
            <div className="w-full">
              {/* forceRunning：消息未落前恒「正在执行」（过程节点全完成≠回复完成；
                  完成态与总时长由固化后的回复卡片承担） */}
              <ProcessBlock nodes={item.nodes} forceRunning />
              {progressTexts[
                `${normalizeProjectScope(projectId)}::${item.threadId}::${item.roleId}`
              ] ? (
                <p
                  data-testid="live-employee-text"
                  className="text-chat-ink mt-1 whitespace-pre-wrap break-words text-sm"
                >
                  {
                    progressTexts[
                      `${normalizeProjectScope(projectId)}::${item.threadId}::${item.roleId}`
                    ]
                  }
                </p>
              ) : null}
              {waitingForUser ? (
                <p
                  data-testid="live-progress-waiting"
                  className="text-chat-muted mt-1 flex items-center gap-1 text-xs"
                >
                  <span className="bg-primary/60 size-1.5 animate-pulse rounded-full" />
                  {t("chat.process.waitingUser")}
                </p>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}