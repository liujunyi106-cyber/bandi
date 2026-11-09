/**
 * 功能：消息列表。
 * 锚定策略：用户位于底部时新消息/流式增量自动滚底；用户上滚阅读时不打扰（stick 关闭）。
 * - 切换会话（sessionKey 变化）→ 永远先定位最新（底部/正在执行消息），不从顶部开始；
 * - 容器/内容尺寸变化（决策卡出现消失、窗口缩放）→ 贴底保持，防消息面板跳动；
 * - 不在最新消息时显示「回到底部」圆形按钮（对话框上方 20px；回到最新后消失）。
 * 每项带 data-chat-anchor-key（规范锚点），供后续精确定位/跳转。
 */
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown } from "lucide-react";
import { Fragment, useEffect, useLayoutEffect, useRef, useState } from "react";

import { MessageItem } from "@/components/chat/MessageItem";
import { useT } from "@/i18n";
import type { AssistantIdentity } from "@/lib/employee";
import type { WorkItemSummary } from "@/services/company";
import type { ChatMessage } from "@/stores/chat-store";

const STICK_THRESHOLD_PX = 80;
/** 超过该条数才启用虚拟化——短会话保持原样（测试/首屏零差异）。 */
const VIRTUALIZE_THRESHOLD = 40;

interface MessageListProps {
  messages: ChatMessage[];
  /** 功能：会话标识（变化即切换会话 → 直接定位最新消息）。 */
  sessionKey?: string;
  onRevertStage?: (entryId: string) => void;
  onFeedback?: (messageId: string, value: "up" | null) => void;
  /** 功能：编辑已发送消息并从此处重新开始对话（透传 MessageItem，-3）。 */
  onEditResend?: (messageId: string, text: string) => void | Promise<boolean>;
  /** 功能：失败工具节点级重试（透传 MessageItem，-3b）。 */
  onRetryTool?: (toolCallId: string) => void;
  /** 功能：本地跳过失败工具节点（透传 MessageItem，-3b）。 */
  onSkipTool?: (nodeId: string) => void;
  /** 功能：重发本轮（失败工具所在轮；透传 MessageItem，-3b）。 */
  onResendTurn?: (messageId: string) => void;
  /** 功能：重新生成助手回复（截断到上一条 user 之前并重发；透传 MessageItem）。 */
  onRegenerate?: (messageId: string) => void;
  /** 功能：工具明细行 → 右栏详情（v5 ，透传 MessageItem）。 */
  onToolDetail?: (messageId: string, nodeId: string) => void;
  /** 功能：已上传附件 → 右栏预览页签（v5 ，透传 MessageItem）。 */
  onAttachmentOpen?: (uploadId: string, name: string) => void;
  /** 功能：@提及 → 右栏员工视图（v5 ，透传 MessageItem）。 */
  onMentionClick?: (name: string) => void;
  /** 功能：@提及高亮名单（v5 ——ChatView 传员工名/角色 id）。 */
  mentions?: string[];
  /** 功能：默认助手身份（秘书会话 = 「秘书」，透传 MessageItem——）。 */
  assistantIdentity?: AssistantIdentity | null;
  /** 功能：成员任务卡（messageId → 该回复派发的子卡；员工回复下方渲染）。 */
  memberTasks?: Record<string, WorkItemSummary[]>;
  /** 功能：交付物卡片（messageId → 该回复产出的文件；点击开右栏预览）。 */
  replyArtifacts?: Record<string, SessionArtifactLike[]>;
  /** 功能：交付物点击 → 右栏产物预览。 */
  onArtifactOpen?: (artifactId: string, name: string, path: string) => void;
  /** 功能：消息流尾部插槽——进行中员工过程等实时内容随消息滚动，
   *  不固定挂在消息区下方挤占空间。 */
  children?: React.ReactNode;
}

/** 交付物 chip 的最小字段面。 */
export interface SessionArtifactLike {
  id: string;
  path: string;
  tool_name?: string;
}

export function MessageList({
  messages,
  sessionKey = "",
  onRevertStage,
  onFeedback,
  onEditResend,
  onRetryTool,
  onSkipTool,
  onResendTurn,
  onRegenerate,
  onToolDetail,
  onAttachmentOpen,
  onMentionClick,
  mentions,
  assistantIdentity,
  memberTasks,
  replyArtifacts,
  // 交付物点击统一走 MessageItem 内的文件页签（保留接口兼容调用方）
  onArtifactOpen: _onArtifactOpen,
  children,
}: MessageListProps) {
  const t = useT();
  const containerRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const lastMessage = messages[messages.length - 1];
  const streamTick = lastMessage ? `${lastMessage.id}:${lastMessage.content.length}` : "";
  // 长会话虚拟化——只渲染视口附近的条目，流式期间不再每帧重排全列表。
  const shouldVirtualize = messages.length > VIRTUALIZE_THRESHOLD;
  const virtualizer = useVirtualizer({
    count: shouldVirtualize ? messages.length : 0,
    getScrollElement: () => containerRef.current,
    estimateSize: () => 120,
    overscan: 6,
    gap: 16,
  });

  const handleScroll = () => {
    const element = containerRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    const stick = distance <= STICK_THRESHOLD_PX;
    stickRef.current = stick;
    setAtBottom(stick);
  };

  // 新消息/流式增量贴底（用户上滚时不打扰）——layout effect：paint 前滚动，防首帧可见的顶部闪烁
  useLayoutEffect(() => {
    if (!stickRef.current) return;
    const element = containerRef.current;
    if (!element) return;
    if (shouldVirtualize && messages.length > 0) {
      virtualizer.scrollToIndex(messages.length - 1, { align: "end" });
    } else {
      element.scrollTop = element.scrollHeight;
    }
  }, [messages.length, streamTick, shouldVirtualize, virtualizer]);

  // 切换会话 → 永远先定位最新（底部/正在执行消息）——layout effect 防首帧闪烁
  useLayoutEffect(() => {
    const element = containerRef.current;
    if (element) element.scrollTop = element.scrollHeight;
    stickRef.current = true;
    setAtBottom(true);
  }, [sessionKey]);

  // 容器/内容尺寸变化（决策卡出现消失等）→ 贴底保持，防面板跳动
  useEffect(() => {
    const element = containerRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (stickRef.current) element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    const content = element.firstElementChild;
    if (content) observer.observe(content);
    return () => observer.disconnect();
  }, []);

  const renderMessage = (message: ChatMessage, index: number) => {
    // 消息级总执行时长：assistant 「已完成 · 时间」=
    // 上一条 user 消息 → 本条回复落卡的墙钟差；缺任一时间戳则不传（回退过程累计）
    let turnDurationMs: number | undefined;
    if (message.role === "assistant" && message.createdAt) {
      for (let back = index - 1; back >= 0; back -= 1) {
        const previous = messages[back];
        if (previous.role === "user") {
          if (previous.createdAt) {
            const delta = Date.parse(message.createdAt) - Date.parse(previous.createdAt);
            if (Number.isFinite(delta) && delta > 0) turnDurationMs = delta;
          }
          break;
        }
      }
    }
    return (
      <MessageItem
        // 本地用户消息也显示操作条（复制/编辑对已发出内容有效）；
        // 本地/流式 assistant 仍不显示（避免操作未定稿内容）
        interactive={message.done && (!message.local || message.role === "user")}
        onRevertStage={onRevertStage}
        onFeedback={(value) => onFeedback?.(message.id, value)}
        onEditResend={onEditResend}
        onRetryTool={onRetryTool}
        onSkipTool={onSkipTool}
        onResendTurn={onResendTurn ? () => onResendTurn(message.id) : undefined}
        onRegenerate={onRegenerate}
        onToolDetail={onToolDetail}
        onAttachmentOpen={onAttachmentOpen}
        onMentionClick={onMentionClick}
        mentions={mentions}
        assistantIdentity={assistantIdentity}
        message={message}
        // 成员任务卡（数据版——完整任务内容可展开；置于正文之后）。
        // 数据卡在场时正文「## 成员任务」文本段不再重复渲染（组件内裁定）。
        memberTaskItems={memberTasks?.[message.id]}
        replyArtifacts={replyArtifacts?.[message.id]}
        turnDurationMs={turnDurationMs}
      />
    );
  };

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto px-4 py-4"
        data-testid="message-list"
      >
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-4">
          {shouldVirtualize ? (
            <div
              style={{ height: virtualizer.getTotalSize(), position: "relative", width: "100%" }}
            >
              {virtualizer.getVirtualItems().map((virtualRow) => {
                const message = messages[virtualRow.index];
                return (
                  <div
                    key={message.id}
                    data-index={virtualRow.index}
                    ref={virtualizer.measureElement}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      transform: `translateY(${virtualRow.start}px)`,
                    }}
                  >
                    {renderMessage(message, virtualRow.index)}
                  </div>
                );
              })}
            </div>
          ) : (
            messages.map((message, index) => (
              <Fragment key={message.id}>{renderMessage(message, index)}</Fragment>
            ))
          )}
          {children}
        </div>
      </div>
      {/* 回到底部（不在最新消息时显示；点击滚底并消失） */}
      {!atBottom ? (
        <button
          type="button"
          data-testid="scroll-to-bottom"
          aria-label={t("chat.scrollToLatest")}
          title={t("chat.scrollToLatest")}
          className="border-chat-hairline bg-card text-chat-muted hover:text-chat-ink absolute bottom-5 left-1/2 z-10 flex size-8 -translate-x-1/2 items-center justify-center rounded-full border shadow-md"
          onClick={() => {
            const element = containerRef.current;
            if (element) element.scrollTop = element.scrollHeight;
            stickRef.current = true;
            setAtBottom(true);
          }}
        >
          <ArrowDown className="size-4" />
        </button>
      ) : null}
    </div>
  );
}
