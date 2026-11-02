/**
 * 功能：会话页路由分流（@base docs/前端设计规划.md §6 会话行 IA）。
 * 默认 = 对话 ChatView（项目会话，团队群聊也由此承载——.3 已删群聊独立视图）；
 * `?view=dm&target=` 员工单聊基础视图（Day137 完整增强）。
 */
import { useSearchParams } from "react-router-dom";

import { ChatView } from "@/components/chat/ChatView";
import { DmChatView } from "@/components/chat/DmChatView";

/** 功能：会话首页（单页壳默认路由，路由参数切换视图）。 */
export default function ChatPage() {
  const [searchParams] = useSearchParams();
  const view = searchParams.get("view");
  if (view === "dm")
    return (
      <DmChatView
        targetRole={searchParams.get("target") ?? ""}
        projectId={searchParams.get("project") ?? ""}
      />
    );
  return <ChatView />;
}
