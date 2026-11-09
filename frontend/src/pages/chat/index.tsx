/**
 * 功能：会话页路由分流。
 * 默认 = 对话 ChatView（项目会话，团队群聊也由此承载——.3 已删群聊独立视图）；
 * `?view=dm&target=` 员工单聊基础视图。
 * 历史 `employee_dm` 会话（旧入口落库的数据）也强制进 DmChatView，避免被当成助理流。
 */
import { useSearchParams } from "react-router-dom";

import { ChatView } from "@/components/chat/ChatView";
import { DmChatView } from "@/components/chat/DmChatView";
import { useChatStore } from "@/stores/chat-store";
import { isEmployeeDmSession } from "@/services/sessions";

/** 功能：会话首页（单页壳默认路由，路由参数切换视图）。 */
export default function ChatPage() {
  const [searchParams] = useSearchParams();
  const view = searchParams.get("view");
  const activeSession = useChatStore((state) => {
    const id = state.activeSessionId;
    return id ? state.sessions.find((item) => item.id === id) : undefined;
  });
  const legacyDm = Boolean(activeSession && isEmployeeDmSession(activeSession));
  if (view === "dm" || legacyDm) {
    return (
      <DmChatView
        targetRole={searchParams.get("target") ?? activeSession?.target_role ?? ""}
        employeeId={activeSession?.employee_id ?? searchParams.get("employee") ?? ""}
        projectId={searchParams.get("project") ?? activeSession?.project_id ?? ""}
      />
    );
  }
  return <ChatView />;
}
