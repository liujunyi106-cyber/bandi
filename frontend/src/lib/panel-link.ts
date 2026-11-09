/**
 * 功能：右栏联动辅助——@提及点击：
 * 从事件目标解析 data-mention-name（MarkdownRenderer 渲染期包裹）→ 名字反查员工 → 聚焦右栏员工视图。
 * 群聊/单聊/会话三视图共用（挂各自根容器的事件委托）。
 */
import type { MouseEvent } from "react";

import { findEmployeeByName } from "@/lib/employee";
import type { EmployeeSummary } from "@/services/company";
import { usePanelStore } from "@/stores/panel-store";

/** 功能：@提及点击 → 员工视图（未命中员工名时忽略）。 */
export function handleMentionClick(
  event: MouseEvent<HTMLElement>,
  employees: EmployeeSummary[],
): void {
  const node = (event.target as HTMLElement).closest<HTMLElement>("[data-mention-name]");
  const name = node?.dataset.mentionName;
  if (!name) return;
  const employee = findEmployeeByName(employees, name);
  if (employee) usePanelStore.getState().focusEmployee(employee.role_id);
}
