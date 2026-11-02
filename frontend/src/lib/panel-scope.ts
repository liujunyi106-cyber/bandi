/**
  * 功能：右栏面板作用域（.2，@base docs/前端设计规划.md）——右栏在两种路由形态下
 * 可展开：会话（session:<会话id>）与项目内员工单聊（employee:<项目>::<角色>）。
 * 作用域键用于宽度/自动展开记忆与页签隔离；解析只读路由参数与当前会话/项目态。
 */
import type { PanelView } from "@/stores/panel-store";

/** 功能：右栏面板作用域（会话 / 项目内员工单聊两形态）。 */
export type PanelScope =
  | { kind: "session"; key: string; sessionId: string }
  | { kind: "employee"; key: string; projectId: string; roleId: string };

export function sessionPanelScope(sessionId: string): PanelScope | null {
  // 功能：会话作用域——空会话无作用域
  const id = sessionId.trim();
  if (!id) return null;
  return { kind: "session", key: `session:${id}`, sessionId: id };
}

export function employeePanelScope(projectId: string, roleId: string): PanelScope | null {
  // 功能：员工作用域——角色缺失无作用域（项目可空，键保留空位）
  const role = roleId.trim();
  if (!role) return null;
  const project = projectId.trim();
  return {
    kind: "employee",
    key: `employee:${project}::${role}`,
    projectId: project,
    roleId: role,
  };
}

export function resolvePanelScope(input: {
  search: string;
  sessionId: string;
  projectId: string;
}): PanelScope | null {
  // 功能：路由解析——DM 路由 → 员工作用域；其余路由 → 会话作用域
  const params = new URLSearchParams(input.search);
  if (params.get("view") === "dm") {
    return employeePanelScope(params.get("project") || input.projectId, params.get("target") ?? "");
  }
  return sessionPanelScope(input.sessionId);
}

export function panelViewsForScope(scope: PanelScope | null): PanelView[] {
  // 功能：视图集——员工作用域 = 概览/文件（无员工/详情）；其余（会话/空）= 四视图全量
  if (scope?.kind === "employee") return ["overview", "files"];
  return ["overview", "employees", "files", "details"];
}
