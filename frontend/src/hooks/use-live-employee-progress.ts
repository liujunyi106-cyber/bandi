/**
 * 功能：订阅员工过程槽的**进行中**视图（按员工分组，供会话视图实时渲染）。
 *
 * 背景：过程槽（`employeeProgress`）此前只有「回复到达时取走固化」一条消费路径——
 * 执行过程中槽里有节点（思考/工具/skills）却没有任何视图渲染它，用户只看到占位信息，
 * 等回复落地才一次性看到过程块。多员工协作时更糟：只固化了最后落消息的那一个员工。
 *
 * 本 hook 提供补齐的那条路径：实时读槽 → 按 role 分组 → 未固化且仍在跑的先显示。
 * 固化（`consumeEmployeeProgress`）后槽被清空，本 hook 自动不再输出 ⇒ 不重复展示。
 */

import { useMemo } from "react";

import { normalizeProjectScope } from "@/lib/progress-key";
import { useChatStore, type ProcessNode } from "@/stores/chat-store";

/** 功能：一名员工的进行中过程（`nodes` 为空数组表示该员工此刻没有过程节点）。 */
export interface LiveEmployeeProgress {
  /** 员工角色 id（槽键第三段）。 */
  roleId: string;
  /** 本次执行的线程 id（槽键第二段）——同员工多次执行互不覆盖。 */
  threadId: string;
  /** 过程节点（与固化过程块同一 `ProcessNode[]` 结构，可直接喂 `<ProcessBlock>`）。 */
  nodes: ProcessNode[];
}

/** 功能：是否仍在执行（存在 `status === "running"` 的节点）；全部收敛即视为已完成。 */
export function hasRunningNode(nodes: ProcessNode[]): boolean {
  return nodes.some((node) => node.status === "running");
}

/**
 * 功能：取当前项目下**尚未固化**的员工过程，按员工分组。
 *
 * @param projectId 当前视图的项目作用域（空 → 归一化为 `company`）
 * @param threadId 限定线程；缺省空串 = 不按线程过滤（团队会话多员工并行都要看到）
 */
export function useLiveEmployeeProgress(
  projectId: string | null | undefined,
  threadId?: string | null,
): LiveEmployeeProgress[] {
  const progress = useChatStore((state) => state.employeeProgress);
  const scope = normalizeProjectScope(projectId);
  const thread = threadId ?? "";

  return useMemo(() => {
    const grouped = new Map<string, LiveEmployeeProgress>();
    for (const [key, nodes] of Object.entries(progress)) {
      if (nodes.length === 0) continue;
      const [keyScope, keyThread, roleId] = key.split("::");
      if (keyScope !== scope) continue;
      if (thread && keyThread !== thread) continue;
      // 同员工同线程只保留一份（键唯一，此处仅为类型收窄）
      grouped.set(`${keyThread}::${roleId}`, { roleId, threadId: keyThread, nodes });
    }
    // 执行中优先，其次按角色字典序 ⇒ 多员工并发时展示顺序稳定（不随帧到达抖动）
    return [...grouped.values()].sort((a, b) => {
      const runningDiff = Number(hasRunningNode(b.nodes)) - Number(hasRunningNode(a.nodes));
      return runningDiff !== 0 ? runningDiff : a.roleId.localeCompare(b.roleId);
    });
  }, [progress, scope, thread]);
}