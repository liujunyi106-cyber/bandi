/**
  * 功能：右栏员工视图（v5，@base docs/前端设计规划.md）——以员工为主体的工作台
 * 项目会话（.3 删甲后团队群聊由项目会话承载）= 员工列表 → 单击展开（摘要格：
 * 技能/任务/token/SOP + 技能胶囊 + 任务列表）；单聊 = 直出目标员工；普通会话 = 助理卡（秘书）。
 * @员工 → 自动展开该员工并滚动到位。
 * 数据面：员工列表 companyApi.employees；技能 panelApi.skills；SOP panelApi.sop；
 * token panelApi.costDetail；任务 companyApi.workItems(role)（知识库档案绑定查询未接入 → 不设区）。
 */
import { Bot, ChevronRight, Loader2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { AssistantAvatar } from "@/components/chat/AssistantAvatar";
import { compactTokens, SummaryCard } from "@/components/chat/panel/panel-shared";
import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { useT } from "@/i18n";
import {
  boundSkillsFor,
  employeeTone,
  resolveEmployeeName,
  SECRETARY_ROLE_ID,
} from "@/lib/employee";
import { cn } from "@/lib/utils";
import { companyApi, type EmployeeSummary, type WorkItemSummary } from "@/services/company";
import {
  panelApi,
  type CostDetail,
  type EmployeeKnowledgeBase,
  type InstalledSkill,
  type ProjectArtifact,
  type SopProgress,
} from "@/services/panel";
import { useChatStore } from "@/stores/chat-store";
import { usePanelStore } from "@/stores/panel-store";
import { toneClass } from "@/theme";

/** 功能：员工卡胶囊/任务列表上限。 */
const LIST_LIMIT = 8;

/** 功能：员工卡展开区（懒加载——展开时才拉数据；技能由父级共享）。
 * .2：会话维度（SOP/token）无会话时跳过请求显示占位；
 * 项目上下文（projectId）时切项目口径——SOP = 绑定卡数（能力档案 skill 资源）
 * Token = 该员工在本项目用量、任务按项目收敛、新增产物小节。
  * 形态对齐概览。 */
function CardDetails({
  sessionId,
  roleId,
  projectId,
  skills,
  indent = true,
}: {
  sessionId: string;
  roleId: string;
  projectId?: string;
  skills: InstalledSkill[] | null;
  indent?: boolean;
}) {
  const t = useT();
  const [sop, setSop] = useState<SopProgress | null>(null);
  const [cost, setCost] = useState<CostDetail | null>(null);
  const [workItems, setWorkItems] = useState<WorkItemSummary[] | null>(null);
  // 员工个体知识库（私有桶）：null = 加载中 / 数组 = 结果（空数组 = 无）
  const [knowledge, setKnowledge] = useState<EmployeeKnowledgeBase[] | null>(null);
  // 项目口径三件：绑定卡数（SOP 格）/ 本项目 token / 本项目产物
  const [bindingCount, setBindingCount] = useState<number | null>(null);
  const [projectTokens, setProjectTokens] = useState<number | null>(null);
  const [artifacts, setArtifacts] = useState<ProjectArtifact[] | null>(null);
  const loadedRef = useRef(false);
  useEffect(() => {
    if (loadedRef.current) return;
    loadedRef.current = true;
    // 会话维度数据（SOP/token）：员工单聊概览无会话 id（.2）——跳过请求显示占位
    if (sessionId) {
      void panelApi
        .sop(sessionId)
        .then((result) => setSop(result.sop))
        .catch(() => undefined);
      void panelApi
        .costDetail(sessionId)
        .then((result) => setCost(result))
        .catch(() => undefined);
    }
    // 项目口径（员工在本项目——绑定卡数 / token / 产物）
    if (projectId) {
      // token 走**会话窗口**：有会话时按其线程过滤，避免显示项目历史累计
      const usageRequest =
        sessionId && typeof companyApi.threads === "function"
          ? companyApi
              .threads(projectId)
              .then(
                (result) =>
                  result.threads.find((thread) => thread.session_id === sessionId)?.thread_id ?? "",
              )
              .catch(() => "")
              .then((threadId) =>
                threadId
                  ? panelApi.projectUsage(projectId, roleId, threadId)
                  : panelApi.projectUsage(projectId, roleId),
              )
          : panelApi.projectUsage(projectId, roleId);
      void usageRequest
        .then((result) => setProjectTokens(result.total_tokens))
        .catch(() => undefined);
      void panelApi
        .projectArtifacts(projectId, roleId)
        .then((result) => setArtifacts(result.artifacts))
        .catch(() => setArtifacts([]));
      void panelApi
        .employeeProfile(roleId)
        .then((result) =>
          setBindingCount(
            result.profile.resources.filter((item) => item.resource_type === "skill").length,
          ),
        )
        .catch(() => setBindingCount(null));
    }
    if (roleId) {
      void panelApi
        .employeeKnowledge(roleId)
        .then((result) => setKnowledge(result.bases))
        .catch(() => setKnowledge([]));
      void companyApi
        .workItems(roleId)
        .then((result) => {
          // 项目上下文按 project_id 收敛（旧卡无归属字段时保底显示，防历史任务消失）；
          // 员工任务面=自己的任务——会话卡（chat_*）是对话记录，不进任务列表
          const items = (
            projectId
              ? result.items.filter((item) => item.project_id === projectId || !item.project_id)
              : result.items
          ).filter((item) => item.source !== "chat_mention" && item.source !== "chat_dm");
          setWorkItems(items);
        })
        .catch(() => setWorkItems([]));
    } else {
      setKnowledge([]);
      setWorkItems([]);
    }
  }, [sessionId, roleId, projectId]);

  const sopValue = projectId
    ? bindingCount === null
      ? "—"
      : String(bindingCount)
    : sop
      ? sop.active
        ? (sop.active.node_id ?? sop.active.skill_id)
        : `${t("chat.panel.sopCompleted")} ${sop.completed_count}`
      : "—";

  return (
    <div
      className={cn("space-y-1.5 pt-1.5 pb-1", indent && "pl-8")}
      data-testid="employee-card-details"
    >
      <div className="grid grid-cols-2 gap-1.5">
        <SummaryCard
          label={t("chat.panel.employeeSkills")}
          value={skills === null ? "—" : String(skills.length)}
        />
        <SummaryCard
          label={t("chat.panel.summaryTokens")}
          value={
            projectId
              ? projectTokens === null
                ? "—"
                : compactTokens(projectTokens)
              : cost
                ? compactTokens(cost.total_tokens)
                : "—"
          }
        />
        <SummaryCard label={t("chat.panel.employeeSop")} value={sopValue} />
        <SummaryCard
          label={t("chat.panel.employeeKnowledge")}
          value={knowledge === null ? "…" : String(knowledge.length)}
          hint={
            knowledge?.length
              ? knowledge
                  .map((item) => item.name || item.base_id)
                  .slice(0, 2)
                  .join(" · ")
              : undefined
          }
        />
      </div>
      {/* 技能列表（字段行——2026-09-26 用户拍板：胶囊语义不清，改与任务列表同款文字行） */}
      <div className="space-y-0.5" data-testid="employee-skills">
        <p className="text-chat-muted text-[10px] font-medium">{t("chat.panel.employeeSkills")}</p>
        {skills === null ? (
          <p className="text-chat-muted text-[11px]">…</p>
        ) : skills.length === 0 ? (
          <p className="text-chat-muted text-[11px]">{t("chat.panel.employeeSkillsEmpty")}</p>
        ) : (
          <ul className="space-y-0.5">
            {skills.slice(0, LIST_LIMIT).map((skill) => (
              <li key={skill.name} className="text-chat-ink truncate text-[11px]">
                {skill.name}
              </li>
            ))}
            {skills.length > LIST_LIMIT ? (
              <li className="text-chat-muted text-[10px]">+{skills.length - LIST_LIMIT}</li>
            ) : null}
          </ul>
        )}
      </div>
      {workItems && workItems.length > 0 ? (
        <div className="space-y-0.5">
          <p className="text-chat-muted text-[10px] font-medium">{t("chat.panel.employeeTasks")}</p>
          <ul className="space-y-0.5">
            {workItems.slice(0, 5).map((item) => (
              <li key={item.id} className="text-chat-ink flex items-center gap-1.5 text-[11px]">
                <span
                  className={cn(
                    "size-1.5 shrink-0 rounded-full",
                    item.column === "done" ? "bg-emerald-500" : "bg-primary/60",
                  )}
                />
                <span className="min-w-0 flex-1 truncate">{item.title}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {/* B3：员工在本项目产物（role 口径聚合） */}
      {artifacts && artifacts.length > 0 ? (
        <div className="space-y-0.5" data-testid="employee-artifacts">
          <p className="text-chat-muted text-[10px] font-medium">
            {t("chat.panel.employeeArtifacts")}
          </p>
          <ul className="space-y-0.5">
            {artifacts.slice(0, 5).map((artifact) => (
              <li
                key={artifact.id}
                className="text-chat-ink truncate font-mono text-[11px]"
                title={artifact.path}
              >
                {artifact.path}
              </li>
            ))}
            {artifacts.length > 5 ? (
              <li className="text-chat-muted text-[10px]">+{artifacts.length - 5}</li>
            ) : null}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

export function EmployeeRow({
  name,
  roleId,
  description,
  metadata,
  sessionId,
  projectId,
  skills,
  expanded,
  onToggle,
  scrollOnMount,
}: {
  name: string;
  roleId: string;
  description?: string;
  metadata?: Record<string, unknown>;
  sessionId: string;
  projectId?: string;
  skills: InstalledSkill[] | null;
  expanded: boolean;
  onToggle: () => void;
  scrollOnMount?: boolean;
}) {
  return (
    <div
      data-testid={`panel-employee-${roleId || "assistant"}`}
      ref={
        scrollOnMount
          ? (node) => {
              // 老环境/测试无 scrollIntoView：可选调用（定位为增强，不因此崩）
              node?.scrollIntoView?.({ block: "nearest" });
            }
          : undefined
      }
      className={cn("rounded-lg px-1.5 py-1", expanded && "bg-chat-rail/40")}
    >
      <button
        type="button"
        data-testid={`panel-employee-open-${roleId || "assistant"}`}
        className="flex w-full items-center gap-1.5 text-left"
        onClick={onToggle}
      >
        {roleId === SECRETARY_ROLE_ID ? (
          <AssistantAvatar className="size-6" />
        ) : roleId && metadata ? (
          <EmployeeAvatar
            name={name}
            metadata={metadata}
            className="size-6 rounded-full text-[10px]"
          />
        ) : (
          <span
            className={cn(
              "flex size-6 shrink-0 items-center justify-center rounded-full text-[10px] font-medium",
              roleId ? toneClass(employeeTone(roleId)) : "bg-primary/10 text-primary",
            )}
          >
            {roleId ? name.slice(0, 1) : <Bot className="size-3.5" />}
          </span>
        )}
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="text-chat-ink min-w-0 truncate text-xs">{name}</span>
          {description ? (
            <span className="text-chat-muted min-w-0 truncate text-[10px]">{description}</span>
          ) : null}
        </span>
        <ChevronRight
          className={cn(
            "text-chat-muted size-3 shrink-0 transition-transform",
            expanded && "rotate-90",
          )}
        />
      </button>
      {expanded ? (
        <CardDetails sessionId={sessionId} roleId={roleId} projectId={projectId} skills={skills} />
      ) : null}
    </div>
  );
}

export function EmployeesView({ sessionId }: { sessionId: string }) {
  const t = useT();
  const [searchParams] = useSearchParams();
  const mode = searchParams.get("view");
  const targetRole = searchParams.get("target") ?? "";
  // 单聊路由的项目上下文（员工数据面切项目口径——绑定卡数/项目 token/产物）
  const dmProjectId = searchParams.get("project") ?? "";
  const expandRoleId = usePanelStore((state) => state.expandRoleId);
  // v5：团队会话（项目上下文）显示员工列表（可伸缩）
  const activeProjectId = useChatStore((state) => state.activeProjectId);
  const [employees, setEmployees] = useState<EmployeeSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [skills, setSkills] = useState<InstalledSkill[] | null>(null);
  const [expandedRole, setExpandedRole] = useState("");
  const [assistantOpen, setAssistantOpen] = useState(true);
  // 项目在职员工（项目成员口径——employee_status 非 inactive；员工档案补描述）
  const [projectMembers, setProjectMembers] = useState<EmployeeSummary[] | null>(null);

  useEffect(() => {
    if (!activeProjectId) {
      setProjectMembers(null);
      return;
    }
    let cancelled = false;
    companyApi
      .projects()
      .then(({ projects }) => {
        if (cancelled) return;
        const project = projects.find((item) => item.project_id === activeProjectId);
        setProjectMembers(
          (project?.members ?? [])
            .filter((member) => member.employee_status !== "inactive")
            .map((member) => ({
              employee_id: member.employee_id,
              name: member.employee_name || resolveEmployeeName(employees, member.role_id),
              role_id: member.role_id,
              description: employees.find((item) => item.role_id === member.role_id)?.description,
              metadata: employees.find((item) => item.role_id === member.role_id)?.metadata,
              status: member.employee_status,
              // 档案技能绑定随员工带出——右栏技能面按员工区分（非全量）
              skills: employees.find((item) => item.role_id === member.role_id)?.skills,
            })),
        );
      })
      .catch(() => {
        if (!cancelled) setProjectMembers([]);
      });
    return () => {
      cancelled = true;
    };
  }, [activeProjectId, employees]);

  useEffect(() => {
    let cancelled = false;
    const loadEmployees = () => {
      companyApi
        .employees()
        .then((result) => {
          if (!cancelled) setEmployees(result.employees);
        })
        .catch(() => undefined)
        .finally(() => {
          if (!cancelled) setLoaded(true);
        });
    };
    loadEmployees();
    window.addEventListener("bandi:employees-changed", loadEmployees);
    return () => {
      cancelled = true;
      window.removeEventListener("bandi:employees-changed", loadEmployees);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
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

  // @员工 → 自动展开该人
  useEffect(() => {
    if (expandRoleId) setExpandedRole(expandRoleId);
  }, [expandRoleId]);

  const list = useMemo(() => employees, [employees]);

  if (!loaded) {
    return (
      <p className="text-chat-muted flex items-center gap-1.5 px-3 py-2 text-xs">
        <Loader2 className="size-3 animate-spin" />
        {t("chat.panel.previewLoading")}
      </p>
    );
  }

  // 单聊直出：只显示目标员工（展开态；项目口径数据面）
  if (mode === "dm" && targetRole) {
    return (
      <div className="px-2 py-2" data-testid="panel-employees">
        <EmployeeRow
          name={resolveEmployeeName(list, targetRole)}
          roleId={targetRole}
          description={list.find((item) => item.role_id === targetRole)?.description}
          metadata={list.find((item) => item.role_id === targetRole)?.metadata}
          sessionId={sessionId}
          projectId={dmProjectId || undefined}
          skills={boundSkillsFor(skills, list.find((item) => item.role_id === targetRole)?.skills)}
          expanded
          onToggle={() => undefined}
        />
      </div>
    );
  }

  // 项目会话：项目在职员工逐个排列
  // 保留展开箭头（EmployeeRow 内置 ChevronRight）与项目口径数据面
  if (mode !== "dm" && activeProjectId) {
    const roster = projectMembers ?? [];
    return (
      <div className="space-y-0.5 px-2 py-2" data-testid="panel-employees">
        {roster.length === 0 ? (
          <p className="text-chat-muted px-1 py-1 text-xs">{t("chat.panel.employeesEmpty")}</p>
        ) : (
          roster.map((employee) => (
            <EmployeeRow
              key={employee.role_id}
              name={employee.name}
              roleId={employee.role_id}
              description={employee.description}
              metadata={employee.metadata}
              sessionId={sessionId}
              projectId={activeProjectId}
              skills={boundSkillsFor(skills, employee.skills)}
              expanded={expandedRole === employee.role_id}
              onToggle={() =>
                setExpandedRole((value) => (value === employee.role_id ? "" : employee.role_id))
              }
              scrollOnMount={expandRoleId === employee.role_id}
            />
          ))
        )}
      </div>
    );
  }

  // 普通会话：助理卡 =「秘书」（第八轮：本项目全能助手即秘书
  // 不存在「AI 助手」；可展开/收起—— ）
  return (
    <div className="px-2 py-2" data-testid="panel-employees">
      <EmployeeRow
        name={t("chat.secretary.name")}
        roleId={SECRETARY_ROLE_ID}
        description={t("chat.panel.assistantRole")}
        sessionId={sessionId}
        skills={skills}
        expanded={assistantOpen}
        onToggle={() => setAssistantOpen((value) => !value)}
      />
    </div>
  );
}
