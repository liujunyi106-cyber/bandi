/**
 * 功能：首屏数据预热——把「后端已就绪」和「界面数据已就绪」合成一次加载。
 *
 * 壳判定 sidecar 的 /ready 通过就导航进 SPA，而会话列表、员工、项目是各组件
 * 挂载后自己拉。不做预热，用户看到的是：启动动画播完 → 进桌面端 → 白屏 → 数据
 * 才到。这里在放行前把首屏必需数据拉完，让启动动画覆盖真实加载时间。
 *
 * 失败不阻断：任一请求失败也放行，组件自身的重试仍会兜底——不能因为一个可选
 * 面板的数据拿不到就把用户卡在启动页。
 */

import { companyApi } from "@/services/company";
import { sessionsApi } from "@/services/sessions";
import { useChatStore } from "@/stores/chat-store";

export interface WarmupResult {
  sessions: number;
  employees: number;
  projects: number;
  failed: boolean;
}

/** 功能：拉一次首屏必需的列表数据并写入 store；全部完成（无论成败）后返回。 */
export async function warmupFirstScreen(): Promise<WarmupResult> {
  const result: WarmupResult = { sessions: 0, employees: 0, projects: 0, failed: false };
  const [sessions, employees, projects] = await Promise.allSettled([
    sessionsApi.list(),
    companyApi.employees(),
    companyApi.projects(),
  ]);

  if (sessions.status === "fulfilled") {
    const list = sessions.value ?? [];
    result.sessions = list.length;
    useChatStore.getState().setSessions(list);
  } else {
    result.failed = true;
  }
  if (employees.status === "fulfilled") {
    result.employees = (employees.value?.employees ?? []).length;
  } else {
    result.failed = true;
  }
  if (projects.status === "fulfilled") {
    result.projects = (projects.value?.projects ?? []).length;
  } else {
    result.failed = true;
  }
  return result;
}
