/**
 * 功能：新建会话导航（左栏「新建会话」与顶栏收起态新建按钮共用）。
 *
 * 归属口径（2026-10-09 用户拍板）：**通用「新建会话」= 普通会话（未选中项目）**——
 * 此前在项目/单聊上下文下按上下文回跳，导致「没有任何普通会话时，用户永远建不出普通会话」。
 * 项目内会话改由项目面板「秘书 / 团队群聊」头像行右侧的新建入口创建，
 * 员工单聊由面板员工行点击进入——两处均已就位，通用按钮不再代偿。
 */
import type { NavigateFunction } from "react-router-dom";

import { useChatStore } from "@/stores/chat-store";

export function startNewSession(navigate: NavigateFunction): void {
  const store = useChatStore.getState();
  store.setActiveSession(null);
  store.setActiveProject("");
  store.setPendingSessionOrigin("");
  navigate("/");
}
