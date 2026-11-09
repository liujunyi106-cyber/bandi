/**
 * 功能：右栏概览视图——数字员工工作台首页：
 * 会话作用域 = 摘要卡（会什么/花了多少/在干什么/产出了什么）+ 计划树（SOP 流程节点 /
 * 团队工作项[带领取者] / 普通 Todo 分流）+ 产出列表（删除独立
 * 「项目团队」板块——团队信息只保留计划段工作项与员工卡数据面）；
 * 员工作用域（.2/）= 单员工卡（数据面：绑定卡数/知识库/项目 Token/任务/产物）。
 */
import { useEffect, useMemo, useState } from "react";

import { EmployeeRow } from "@/components/chat/panel/EmployeesView";
import { useEmployees } from "@/hooks/use-employees";
import { compactTokens, SummaryCard } from "@/components/chat/panel/panel-shared";
import { useT } from "@/i18n";
import { boundSkillsFor, resolveEmployeeName } from "@/lib/employee";
import type { PanelScope } from "@/lib/panel-scope";
import { cn } from "@/lib/utils";
import {
  companyApi,
  findDirectThread,
  type EmployeeSummary,
  type ThreadSummary,
  type WorkItemSummary,
} from "@/services/company";
import { panelApi, type CostDetail, type InstalledSkill, type SopProgress } from "@/services/panel";
import { sessionsApi, type SessionArtifact } from "@/services/sessions";
import { useChatStore, type TaskLedgerItem } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";

const EMPTY_TASKS: TaskLedgerItem[] = [];

const TASK_DOT: Record<TaskLedgerItem["status"], string> = {
  completed: "bg-emerald-500",
  in_progress: "bg-primary",
  pending: "bg-chat-muted/40",
};

function TaskRow({
  item,
  highlighted,
  onFocus,
}: {
  item: TaskLedgerItem;
  highlighted: boolean;
  onFocus: () => void;
}) {
  const t = useT();
  return (
    <button
      type="button"
      data-testid={`panel-task-${item.id}`}
      className={cn(
        "flex w-full items-start gap-1.5 rounded-md px-1.5 py-1 text-left",
        highlighted ? "bg-primary/10" : "hover:bg-primary/5",
      )}
      onClick={onFocus}
    >
      <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", TASK_DOT[item.status])} />
      <span className="min-w-0 flex-1">
        <span
          className={cn(
            "block truncate text-xs",
            item.status === "completed" ? "text-chat-muted line-through" : "text-chat-ink",
          )}
        >
          {item.title}
        </span>
      </span>
      <span className="text-chat-muted shrink-0 text-[10px]">{t(`chat.task.${item.status}`)}</span>
    </button>
  );
}

/** 功能：工作项树（parent_id 分组；无 parent 为根——最多两层缩进展示）。
 * 按承接员工分组：各员工任务独立成节（不再总和展示）。行带状态标签（团队计划表口径）。 */
function WorkItemTree({ items }: { items: WorkItemSummary[] }) {
  const t = useT();
  const { employees } = useEmployees();
  const nameOf = useMemo(() => {
    const map = new Map(
      employees.map((employee) => [employee.role_id, employee.name || employee.role_id]),
    );
    return (roleId: string) => map.get(roleId) ?? roleId;
  }, [employees]);
  const sections = useMemo(() => {
    const childrenOf = new Map<string, WorkItemSummary[]>();
    const roots: WorkItemSummary[] = [];
    const ids = new Set(items.map((item) => item.id));
    for (const item of items) {
      const parent = item.parent_id ?? "";
      if (parent && ids.has(parent)) {
        childrenOf.set(parent, [...(childrenOf.get(parent) ?? []), item]);
      } else {
        roots.push(item);
      }
    }
    const flatOf = (root: WorkItemSummary): Array<{ item: WorkItemSummary; depth: number }> => {
      const flat: Array<{ item: WorkItemSummary; depth: number }> = [];
      const walk = (item: WorkItemSummary, depth: number) => {
        flat.push({ item, depth: Math.min(depth, 2) });
        for (const child of childrenOf.get(item.id) ?? []) walk(child, depth + 1);
      };
      walk(root, 0);
      return flat;
    };
    const grouped = new Map<string, Array<{ item: WorkItemSummary; depth: number }>>();
    for (const root of roots) {
      const key = root.assignee_role || "";
      grouped.set(key, [...(grouped.get(key) ?? []), ...flatOf(root)]);
    }
    return [...grouped.entries()];
  }, [items]);
  return (
    <div className="space-y-1" data-testid="panel-work-items">
      {sections.map(([assignee, rows]) => (
        <div
          key={assignee || "unassigned"}
          data-testid={`panel-work-items-${assignee || "unassigned"}`}
        >
          <p className="text-chat-muted px-1.5 pt-0.5 pb-0.5 text-[10px] font-medium">
            {assignee ? nameOf(assignee) : "未指派"}
          </p>
          <div className="space-y-0.5">
            {rows.map(({ item, depth }) => (
              <div
                key={item.id}
                className="flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs"
                style={{ paddingLeft: 6 + depth * 12 }}
              >
                <span
                  className={cn(
                    "size-1.5 shrink-0 rounded-full",
                    item.column === "done"
                      ? "bg-emerald-500"
                      : item.column === "in_progress"
                        ? "bg-primary"
                        : item.column === "in_review"
                          ? "bg-amber-500"
                          : "bg-chat-muted/40",
                  )}
                />
                <span className="min-w-0 flex-1">
                  <span className="text-chat-ink block truncate">{item.title}</span>
                  {item.last_output_preview ? (
                    <span className="text-chat-muted block truncate text-[10px]">
                      {item.last_output_preview}
                    </span>
                  ) : null}
                </span>
                <span className="text-chat-muted shrink-0 text-[10px]">
                  {item.column === "done"
                    ? t("chat.task.completed")
                    : item.column === "in_progress"
                      ? t("chat.task.in_progress")
                      : item.column === "in_review"
                        ? t("chat.task.in_review")
                        : t("chat.task.pending")}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 功能：SOP 流程节点分组（活动节点突出 + 待处理列表 + 已完成计数）。 */
function SopSection({ sop }: { sop: SopProgress }) {
  const t = useT();
  return (
    <div className="space-y-1" data-testid="panel-sop">
      {sop.active ? (
        <div className="border-primary/30 bg-primary/5 rounded-md border px-2 py-1.5">
          <p className="text-chat-muted text-[10px]">{t("chat.panel.sopCurrent")}</p>
          <p className="text-chat-ink truncate text-xs font-medium">
            {sop.active.node_id ?? sop.active.skill_id}
          </p>
          {sop.active.awaiting_fields.length ? (
            <p className="text-chat-muted mt-0.5 truncate text-[10px]">
              {t("chat.panel.sopAwaiting")}: {sop.active.awaiting_fields.join(", ")}
            </p>
          ) : null}
        </div>
      ) : null}
      {sop.pending.map((frame) => (
        <div key={frame.task_id} className="flex items-center gap-1.5 px-1.5 py-0.5 text-xs">
          <span className="bg-chat-muted/40 size-1.5 shrink-0 rounded-full" />
          <span className="text-chat-muted min-w-0 flex-1 truncate">
            {frame.node_id ?? frame.skill_id}
          </span>
        </div>
      ))}
      {sop.completed_count > 0 ? (
        <p className="text-chat-muted px-1.5 pt-0.5 text-[10px]">
          {t("chat.panel.sopCompleted")}: {sop.completed_count}
        </p>
      ) : null}
    </div>
  );
}

/** 功能：员工单聊概览（.2/）——直出该员工卡（数据面：绑定卡数/知识库/项目 Token/任务/产物）。 */
function EmployeeOverview({ scope }: { scope: Extract<PanelScope, { kind: "employee" }> }) {
  const [employees, setEmployees] = useState<EmployeeSummary[] | null>(null);
  const [skills, setSkills] = useState<InstalledSkill[] | null>(null);
  // 单聊直聊线程：会话级 token/上下文按该线程取，不能回落到项目累计
  const [thread, setThread] = useState<ThreadSummary | null>(null);

  useEffect(() => {
    if (!scope.projectId) {
      setThread(null);
      return;
    }
    let cancelled = false;
    companyApi
      .threads(scope.projectId)
      .then(({ threads }) => {
        if (!cancelled) setThread(findDirectThread(threads, scope.roleId));
      })
      .catch(() => {
        if (!cancelled) setThread(null);
      });
    return () => {
      cancelled = true;
    };
  }, [scope.projectId, scope.roleId]);

  useEffect(() => {
    let cancelled = false;
    companyApi
      .employees()
      .then((result) => {
        if (!cancelled) setEmployees(result.employees);
      })
      .catch(() => undefined);
    panelApi
      .skills()
      .then((result) => {
        if (!cancelled) setSkills(result.skills);
      })
      .catch(() => {
        if (!cancelled) setSkills([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const list = employees ?? [];
  return (
    <div className="px-2 py-2" data-testid="panel-overview">
      <EmployeeRow
        name={resolveEmployeeName(list, scope.roleId)}
        roleId={scope.roleId}
        description={list.find((item) => item.role_id === scope.roleId)?.description}
        metadata={list.find((item) => item.role_id === scope.roleId)?.metadata}
        sessionId={thread?.session_id || ""}
        threadId={thread?.thread_id}
        projectId={scope.projectId}
        skills={boundSkillsFor(skills, list.find((item) => item.role_id === scope.roleId)?.skills)}
        useSessionCost
        expanded
        onToggle={() => undefined}
      />
    </div>
  );
}

export function OverviewView({ scope }: { scope: PanelScope }) {
  // 功能：按作用域分流——员工单聊 = 单员工卡；会话 = 会话工作台首页
  if (scope.kind === "employee") return <EmployeeOverview scope={scope} />;
  return <SessionOverview sessionId={scope.sessionId} />;
}

function SessionOverview({ sessionId }: { sessionId: string }) {
  const t = useT();
  const tasks = useChatStore((state) => state.tasksBySession[sessionId]) ?? EMPTY_TASKS;
  // 每轮对话必变的信号（纯文本轮任务台账不变，用它当触发会漏刷新）
  const messageCount = useChatStore((state) => state.messagesBySession[sessionId]?.length ?? 0);
  // 会话活动态：running→done 由本轮终帧驱动，是「本轮成本已刷库」的信号
  const activity = useChatStore((state) => state.sessionActivity[sessionId]);
  const projectId = useChatStore((state) => state.activeProjectId);
  const taskFocusId = usePanelStore((state) => state.taskFocusId);
  const focusTask = usePanelStore((state) => state.focusTask);
  const focusArtifact = usePanelStore((state) => state.focusArtifact);
  const focusFile = usePanelStore((state) => state.focusFile);
  const [artifacts, setArtifacts] = useState<SessionArtifact[] | null>(null);
  const [cost, setCost] = useState<CostDetail | null>(null);
  const [skillCount, setSkillCount] = useState<number | null>(null);
  const [sop, setSop] = useState<SopProgress | null>(null);
  const [workItems, setWorkItems] = useState<WorkItemSummary[] | null>(null);
  // 30s 轮询重拉 tick——员工卡相位变化无聊天消息伴随时的右栏刷新信号
  const [resyncTick, setResyncTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setResyncTick((value) => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, []);
  // 秘书会话：右栏计划 = 助理自己的 Todo（不展示团队计划/工作项——秘书不承载团队工作流）
  const isSecretary = useChatStore(
    (state) => state.sessions.find((item) => item.id === sessionId)?.origin === "secretary",
  );

  useEffect(() => {
    let cancelled = false;
    setArtifacts(null);
    setCost(null);
    setSkillCount(null);
    setSop(null);
    setWorkItems(null);
    // 产物严格按会话作用域：只列本会话产出的文件。项目级聚合会把其它会话/员工的
    // 历史文件塞进新会话右栏（如 market-entry/R-01/*.md），用户读成"这个会话做过这些"。
    const artifactsRequest = sessionsApi.artifacts(sessionId).then((result) => result.artifacts);
    void artifactsRequest
      .then((items) => {
        if (!cancelled) setArtifacts(items as SessionArtifact[]);
      })
      .catch(() => {
        if (!cancelled) setArtifacts([]);
      });
    panelApi
      .costDetail(sessionId)
      .then((result) => {
        if (!cancelled) setCost(result);
      })
      .catch(() => undefined);
    panelApi
      .sop(sessionId)
      .then((result) => {
        if (!cancelled) setSop(result.sop);
      })
      .catch(() => undefined);
    if (projectId && !isSecretary) {
      // 团队任务 = 本项目工作项（project_id 字段优先，旧卡回落项目成员角色过滤
      // 防全量历史工作项混入；秘书会话不取团队工作项）
      void companyApi
        .projects()
        .then(async (result) => {
          const project = result.projects.find((item) => item.project_id === projectId);
          const memberRoles = new Set((project?.members ?? []).map((member) => member.role_id));
 // 技能数 = **团队员工绑定技能合计**
          void companyApi
            .employees()
            .then((empResult) => {
              if (cancelled) return;
              const bound = new Set<string>();
              for (const employee of empResult.employees) {
                if (memberRoles.size > 0 && !memberRoles.has(employee.role_id)) continue;
                for (const skill of employee.skills ?? []) bound.add(skill);
              }
              setSkillCount(bound.size);
            })
            .catch(() => {
              if (!cancelled) setSkillCount(0);
            });
          const items = await companyApi.workItems();
          // 只展示**当前会话窗口**的工作项：会话 → 线程绑定查询（群聊线程
          // session_id = 会话 id）。此前按项目全量展示，历史自动建链子卡会
          // 以「视觉设计师：本环节交付」等同名条目反复堆叠。
          const threads = await companyApi
            .threads(projectId)
            .then((result) => result.threads)
            .catch(() => []);
          const boundThread = threads.find((thread) => thread.session_id === sessionId);
          const threadId = boundThread?.thread_id ?? "";
          return (
            items.items
              .filter((item) => (threadId ? item.thread_id === threadId : false))
              .filter((item) =>
                item.project_id
                  ? item.project_id === projectId
                  : Boolean(item.assignee_role && memberRoles.has(item.assignee_role)),
              )
              // 团队计划表口径：只展示分工任务（agent_to_agent 等）——
              // 会话卡（chat_mention/chat_dm）是对话记录不进计划树；咨询问答卡
              // （chat_kind=query）同样不出发计划
              .filter(
                (item) =>
                  item.source !== "chat_mention" &&
                  item.source !== "chat_dm" &&
                  item.chat_kind !== "query",
              )
              .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))
          );
        })
        .then((related) => {
          if (!cancelled) setWorkItems(related);
        })
        .catch(() => {
          if (!cancelled) setWorkItems([]);
        });
    } else {
      // 普通会话 / 项目内助理会话：概览同样显示技能数——
      // 无团队上下文时按「全局已装技能库存」计数（与员工概览数据源同源）
      void panelApi
        .skills()
        .then((result) => {
          if (!cancelled) setSkillCount(result.skills.length);
        })
        .catch(() => {
          if (!cancelled) setSkillCount(0);
        });
    }
    return () => {
      cancelled = true;
    };
    // 重拉信号 = 会话/项目切换 + 消息数 + 会话活动态。消息数在流式首字就已增加，
    // 早于成本落库；活动态 done 由终帧驱动，此刻取到的用量含本轮。
    // +30s 轮询 tick：员工卡 phase 变化（待办→进行中→完成）若无对应聊天消息
    // 时右栏不会刷新——轻量轮询补齐「计划状态」实时性
  }, [sessionId, projectId, messageCount, activity, isSecretary, resyncTick]);

  const done = tasks.filter((item) => item.status === "completed").length;
  const artifactList = artifacts ?? [];
  // 功能：计划段口径——SOP 有进行中/待执行节点时独占展示；全部完成时降级为一行摘要，
  // 团队工作项与会话待办照常按优先级展示（工作项优先，其次待办）。
  const sopInFlight = Boolean(sop && (sop.active || sop.pending.length > 0));
  const sopSummary = Boolean(sop && !sopInFlight && sop.completed_count > 0);
  const planWorkItems = !sopInFlight && !isSecretary ? workItems : null;
  const hasWorkItems = Boolean(planWorkItems && planWorkItems.length > 0);
  const hasTodos = !sopInFlight && !hasWorkItems && tasks.length > 0;
  const planEmpty = !sopInFlight && !sopSummary && !hasWorkItems && !hasTodos;

  return (
    <div className="space-y-2 px-2 py-2" data-testid="panel-overview">
      <div className="grid grid-cols-2 gap-1.5">
        <SummaryCard
          label={t("chat.panel.summarySkills")}
          value={skillCount === null ? "—" : String(skillCount)}
        />
        <SummaryCard
          label={t("chat.panel.summaryTokens")}
          value={cost ? compactTokens(cost.total_tokens) : "—"}
        />
        <SummaryCard
          label={t("chat.panel.summaryTasks")}
          value={tasks.length ? `${done}/${tasks.length}` : "—"}
        />
        <SummaryCard
          label={t("chat.panel.summaryArtifacts")}
          value={artifacts === null ? "—" : String(artifactList.length)}
        />
      </div>

      <section data-testid="panel-plan">
        <p className="text-chat-muted px-1 pb-1 text-[11px] font-medium">
          {t("chat.panel.planTitle")}
        </p>
        {/* 功能：任务只在一处呈现——进行中的 SOP > 团队工作项（含领取者）> 会话待办 */}
        {sop && sopInFlight ? <SopSection sop={sop} /> : null}
        {sop && sopSummary ? (
          <p data-testid="panel-sop-summary" className="text-chat-muted px-1.5 py-1 text-xs">
            {t("chat.panel.sopCompleted")}: {sop.completed_count}
          </p>
        ) : null}
        {planWorkItems && planWorkItems.length > 0 ? <WorkItemTree items={planWorkItems} /> : null}
        {hasTodos ? (
          <div data-testid="panel-plan-todo">
            {tasks.map((item) => (
              <TaskRow
                key={item.id}
                item={item}
                highlighted={taskFocusId === item.id}
                onFocus={() => focusTask(item.id)}
              />
            ))}
          </div>
        ) : null}
        {planEmpty ? (
          <p className="text-chat-muted px-1.5 py-1 text-xs">{t("chat.panel.planEmpty")}</p>
        ) : null}
      </section>

      <section data-testid="panel-artifacts">
        <p className="text-chat-muted px-1 pb-1 text-[11px] font-medium">
          {t("chat.panel.artifactsTitle")}
        </p>
        {artifactList.length === 0 ? (
          <p className="text-chat-muted px-1.5 py-1 text-xs">{t("chat.panel.artifactsEmpty")}</p>
        ) : (
          <ul className="space-y-0.5">
            {artifactList.map((artifact) => (
              <li key={artifact.id}>
                <button
                  type="button"
                  data-testid={`panel-artifact-${artifact.id}`}
                  className="hover:bg-primary/5 flex w-full items-center gap-1.5 rounded-md px-1.5 py-1 text-left"
                  onClick={() => {
                    const name = artifact.path.split("/").pop() ?? artifact.path;
                    // 项目聚合产物（右栏「产物」）id 不属于会话 artifacts——按路径直读，
                    // 否则预览端点按 artifact_id 查不到文件报 ARTIFACT_NOT_FOUND
                    if (projectId) {
                      focusFile(artifact.path, name, projectId);
                      return;
                    }
                    focusArtifact(artifact.id, name, artifact.path);
                  }}
                >
                  <span className="text-chat-ink min-w-0 flex-1 truncate font-mono text-xs">
                    {artifact.path}
                  </span>
                  <span className="text-chat-muted shrink-0 text-[10px]">{artifact.tool_name}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
