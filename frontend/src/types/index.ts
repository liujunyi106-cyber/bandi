/**
  * 功能：跨模块共享类型（）。
 * 只放「被多处引用」的契约型定义；单模块私有类型就近声明。
 */

/** 支持的内核连接状态（与 services/ws.ts 的 WsStatus 同源，此处为对外别名）。 */
export type { WsStatus } from "@/services/ws";

/* 员工角色视觉身份 tone（员工色； 第 4 档由品牌蓝改石墨灰）。 */
export type EmployeeTone = "ink" | "copper" | "olive" | "graphite";

/* 危险操作分级（危险操作分级确认）。 */
export type DangerLevel = "L1" | "L2" | "L3";

/* 单页壳可插槽区域（Slot 预留）。 */
export type SlotRegion =
  "sidebar.nav" | "header.center" | "header.right" | "chat.right" | "settings.sections";
