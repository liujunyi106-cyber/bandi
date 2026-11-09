/**
 * 功能：共享会话只读页。
 * 拉取 GET /api/shared/{token}（无需登录凭证的口令式只读视图）→ 消息列表只读渲染。
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { MessageList } from "@/components/chat/MessageList";
import { useT } from "@/i18n";
import { ApiError } from "@/services/api";
import { sessionsApi } from "@/services/sessions";
import { entriesToMessages, type ChatMessage } from "@/stores/chat-store";

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** 功能：共享会话页（只读）。 */
export default function SharedPage() {
  const t = useT();
  const { token = "" } = useParams();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!token) return;
    sessionsApi
      .shared(token)
      .then((result) => setMessages(entriesToMessages(result.session_id, result.messages)))
      .catch((cause) => setError(errorMessage(cause)));
  }, [token]);

  return (
    <div className="bg-chat-bg flex h-svh min-h-0 flex-col">
      <header className="border-chat-hairline flex h-14 shrink-0 items-center px-4">
        <span className="text-chat-ink text-sm font-semibold">{t("shared.title")}</span>
      </header>
      {error ? (
        <div className="text-chat-muted flex flex-1 items-center justify-center text-sm">
          {error}
        </div>
      ) : (
        <MessageList messages={messages} />
      )}
    </div>
  );
}
