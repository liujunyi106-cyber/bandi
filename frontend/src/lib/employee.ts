/**
 * 功能：员工展示工具（头像 tone 稳定分配 + 角色名解析——项目面板/群聊/单聊共用）。
 */
import type { EmployeeSummary } from "@/services/company";
import type { InstalledSkill } from "@/services/panel";
import type { EmployeeTone } from "@/types";

const TONE_POOL: EmployeeTone[] = ["ink", "copper", "olive", "graphite"];

/**
 * 功能：项目秘书的发言身份（与后端 `SECRETARY_ROLE_ID`、项目面板「秘书」入口同标识）。
 * 无团队/员工的项目空间里秘书是唯一接待者——群聊兜底应答即由后端以该 role 产出消息。
 */
export const SECRETARY_ROLE_ID = "secretary";

/** 功能：是否为秘书身份（头像/名称走秘书专属展示）。 */
export function isSecretary(roleId: string): boolean {
  return roleId === SECRETARY_ROLE_ID;
}

/**
 * 功能：默认助手身份（消息身份头/右栏助理卡共用）。
 * 普通会话 = 「AI 助手」；秘书会话 = 「秘书」——与项目面板「秘书」入口同名同 tone。
 */
export interface AssistantIdentity {
  name: string;
  roleId: string;
}

/** 功能：按 role_id 稳定分配员工头像 tone（占位头像底色）。 */
export function employeeTone(roleId: string): EmployeeTone {
  let hash = 0;
  for (const ch of roleId) hash = (hash * 31 + ch.charCodeAt(0)) % 997;
  return TONE_POOL[hash % TONE_POOL.length];
}

/** 功能：role_id → 员工名（查不到回落 role_id 原文——消息渲染不因缺档案而空白）。 */
export function resolveEmployeeName(employees: EmployeeSummary[], roleId: string): string {
  return employees.find((employee) => employee.role_id === roleId)?.name || roleId;
}

/**
 * 功能：员工档案绑定的技能过滤——按 name 匹配注册技能（补描述/标签）；
 * 未注册引用以 name-only 形态保留（档案有绑定但技能未安装时也如实展示）。
 * 返回 null = 注册技能列表尚未加载（卡片显示占位）。
 */
export function boundSkillsFor(
  allSkills: InstalledSkill[] | null,
  refs: string[] | undefined,
): InstalledSkill[] | null {
  if (allSkills === null) return null;
  const wanted = refs ?? [];
  if (wanted.length === 0) return [];
  return wanted.map(
    (ref) =>
      allSkills.find((skill) => skill.name === ref) ?? {
        name: ref,
        description: "",
        tags: [],
        team_id: "",
        always: false,
      },
  );
}

/** 功能：名字/角色 id → 员工（@提及点击联动用——v5 ；未命中返回 undefined）。 */
export function findEmployeeByName(
  employees: EmployeeSummary[],
  name: string,
): EmployeeSummary | undefined {
  return employees.find((employee) => employee.name === name || employee.role_id === name);
}
