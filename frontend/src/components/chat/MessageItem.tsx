/**
 * 功能：单条消息条目（@base docs/前端设计规划.md §8：过程块 + 反馈按钮内嵌消息流）。
 * user 右对齐气泡 / assistant 左对齐（过程块在正文上方）/ system 居中小字；
 * 操作条：时间戳在左（相对日期），复制在最右；
 * user 仅保留「编辑 + 复制」（无点赞/差评/更多/重发），assistant 保留「👍/👎/⋯/复制」；
 * 编辑态：就地编辑已发送内容（提示"从此处重新开始对话，已有产物不会被删除" + 取消/发送）。
 * 正文：assistant 走 MarkdownRenderer（markdown + mermaid），user 走 plain 原样呈现。
 */
import {
  Check,
  Copy,
  Info,
  Loader2,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  RotateCcw,
  ThumbsUp,
} from "lucide-react";
import { memo, useEffect, useMemo, useState } from "react";

import { AssistantAvatar } from "@/components/chat/AssistantAvatar";
import { AttachmentChips } from "@/components/chat/AttachmentChips";
import { CitationChips } from "@/components/chat/CitationChips";
import { EmployeeIdentityHeader } from "@/components/chat/EmployeeIdentityHeader";
import { MarkdownRenderer } from "@/components/chat/MarkdownRenderer";
import { stripGeneratedCitationBlock } from "@/lib/citation-cleanup";
import { MemberTaskCards } from "@/components/chat/MemberTaskCard";
import {
  extractMemberTaskSection,
  MemberTaskTextCards,
} from "@/components/chat/MemberTaskTextCard";
import { ProcessBlock } from "@/components/chat/ProcessBlock";
import { ReplySources } from "@/components/chat/ReplySources";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useT } from "@/i18n";
import { extractReferencedFiles } from "@/lib/chat-references";
import { resolveEmployeeName, type AssistantIdentity } from "@/lib/employee";
import { useEmployees } from "@/hooks/use-employees";
import type { WorkItemSummary } from "@/services/company";
import { shouldUseDocumentLayout } from "@/lib/markdown";
import { formatFullTime, formatRelativeTime } from "@/lib/time";
import { cn } from "@/lib/utils";
import { usePanelStore } from "@/stores/panel-store";
import type { ChatMessage, CitationRef, ProcessNode } from "@/stores/chat-store";

interface MessageItemProps {
  message: ChatMessage;
  /** 是否显示操作（流式中/本地 pending 不显示）。 */
  interactive?: boolean;
  onRevertStage?: (entryId: string) => void;
  onFeedback?: (value: "up" | null) => Promise<void> | void;
  /** 功能：编辑已发送消息并从此处重新开始对话。 */
  onEditResend?: (messageId: string, text: string) => void | Promise<boolean>;
  /** 功能：失败工具节点级重试（-3b，toolCallId = 后端定位键）。 */
  onRetryTool?: (toolCallId: string) => void;
  /** 功能：本地跳过失败工具节点（-3b）。 */
  onSkipTool?: (nodeId: string) => void;
  /** 功能：重发本轮（失败工具所在轮的 user 输入；-3b）。 */
  onResendTurn?: () => void;
  /** 功能：重新生成该条助手回复（截断到其上一条 user 消息之前并重发）。 */
  onRegenerate?: (messageId: string) => void;
  /** 功能：点击工具明细行 → 右栏详情（v5 ）。 */
  onToolDetail?: (messageId: string, nodeId: string) => void;
  /** 功能：点击已上传附件 → 右栏预览页签（v5 ）。 */
  onAttachmentOpen?: (uploadId: string, name: string) => void;
  /** 功能：点击 @提及 → 右栏员工视图（v5 ）。 */
  onMentionClick?: (name: string) => void;
  /** 功能：@提及高亮名单（v5——ChatView 传员工名/角色 id；渲染后 DOM 处理）。 */
  mentions?: string[];
  /** 功能：默认助手身份覆盖（秘书会话 = 「秘书」——与项目面板/右栏一致；缺省 = 「AI 助手」）。
   *  显式 `null` = 不显示身份头（团队会话无署名流式占位——团队里没有「秘书/助手」角色）。 */
  assistantIdentity?: AssistantIdentity | null;
  /** 功能：成员任务卡（该回复派发的子卡——完整任务内容可展开；置于正文之后）。 */
  memberTaskItems?: WorkItemSummary[];
  /** 功能：本轮产物（写类工具落盘文件——与引用文件同一容器渲染，紧凑排布）。 */
  replyArtifacts?: { id: string; path: string; tool_name?: string }[];
  /** 功能：本轮消息级总执行时长（上一条 user 消息 → 本条回复落卡的墙钟差；
   *  过程块总行「已完成 · 时间」的口径来源）。 */
  turnDurationMs?: number;
}

/**
 * 功能：「正在回复中…」的秒级计时。
 *
 * 范围口径（用户已澄清）：**只**给「尝试调用阶段」指示器计时，过程块内部不加实时计时。
 * active 结束后复位为 0，下一轮重新计时。纯文本数字，无动画，`prefers-reduced-motion`
 * 下无需降级；首帧到达后本行即被过程块/正文取代，计时自然消失。
 */
function useElapsedSeconds(active: boolean): number {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!active) {
      setSeconds(0);
      return;
    }
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setSeconds(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return seconds;
}

/** 功能：秒数 → 短标签（<显示 `Ns`，之后显示 `M:SS`）。 */
function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function MessageItemComponent({
  message,
  interactive,
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
  memberTaskItems,
  replyArtifacts,
  turnDurationMs,
}: MessageItemProps) {
  const t = useT();
  // 交接行「下一步：{名字}」角色名解析（员工档案共享缓存）
  const { employees } = useEmployees();
  const senderEmployee = message.senderName
    ? employees.find(
        (employee) =>
          employee.role_id === message.senderRole || employee.name === message.senderName,
      )
    : undefined;
  /** 本轮引用文件（read/grep/glob 路径去重——回复卡片引用行数据源）。 */
  const referencedFiles = extractReferencedFiles(message.process);
  const [copied, setCopied] = useState(false);
  // 编辑态（-3 ：就地编辑已发送的 user 消息）
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const streaming = !message.done && !message.error;

  const citations = useMemo(() => {
    const out: CitationRef[] = [];
    const walk = (nodes?: ProcessNode[]) => {
      for (const node of nodes ?? []) {
        if (node.kind !== "tool") continue;
        if (node.citations?.length) out.push(...node.citations);
        if (node.children?.length) walk(node.children);
      }
    };
    walk(message.process);
    return out;
  }, [message.process]);

  /** 功能：点击引用文件 → 本地路径在右栏开文件页签；外部链接（http/https）新标签跳转。 */
  const openReferencedFile = (path: string) => {
    if (/^https?:\/\//i.test(path)) {
      window.open(path, "_blank", "noopener,noreferrer");
      return;
    }
    usePanelStore.getState().focusFile(path, path.split(/[\\/]/).pop());
  };

  const displayContent = useMemo(
    () => (isUser ? message.content : stripGeneratedCitationBlock(message.content)),
    [isUser, message.content],
  );

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(displayContent);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* 剪贴板不可用（无权限）——静默忽略 */
    }
  };

  const documentLayout = !isUser && shouldUseDocumentLayout(displayContent);
  // 普通回复也带默认身份头（员工回复用员工名，其余用「AI 助手」）
  // 秘书会话覆盖默认身份（消息头「秘书」）
  // 团队会话（assistantIdentity === null 且无署名）= 不显示身份头
  // 有署名但解析不到员工时同样隐藏，避免首字母假头像（W / Worker）。
  const hideIdentity = !isUser
    ? message.senderName
      ? !senderEmployee
      : assistantIdentity === null
    : false;
  const assistantLabel = message.senderName ?? assistantIdentity?.name ?? t("chat.assistant.name");
  // 头恒有 → 内容（过程块/状态行/气泡）统一与头像左缘对齐（不再缩进到姓名位）
  // 空流式期（尚无正文）：状态行承担等待指示；正文未到前不渲染白色空卡
  // 「正在回复中…」占位：仅当流式且无内容**且尚未出现过程链**时显示——
  // 过程链（思考/工具）已出现则由过程块承担「正在执行中」，两者不同时出现
  const emptyStreaming = streaming && !displayContent.trim() && !(message.process?.length ?? 0);
  const elapsed = useElapsedSeconds(emptyStreaming);
  // 员工回复（thread 回写注入）：正文的「## 成员任务」段抽卡渲染
  const isEmployee = !isUser && Boolean(message.senderName);
  // 成员任务卡：契约引导「派任务才写该段」——段在场即抽卡
  // （历史数据普遍无 workItemId 键，按段驱动避免误伤真任务卡）
  const memberTaskSection = useMemo(
    () =>
      isEmployee
        ? extractMemberTaskSection(displayContent)
        : { body: displayContent, section: "" },
    [displayContent, isEmployee],
  );

  if (isSystem) {
    return (
      <div
        className="text-chat-muted flex justify-center py-2 text-xs"
        data-chat-anchor-key={message.id}
      >
        <span className="bg-chat-rail rounded-full px-3 py-0.5">{message.content}</span>
      </div>
    );
  }

  const emptyAssistant = !isUser && !displayContent.trim() && !(message.process?.length ?? 0);
  if (!isUser && message.local === true && emptyAssistant) {
    // 团队/员工会话的本地占位：回复由 thread_message 承担，不渲染空助理气泡/身份头。
    // 助理会话流式空态保留「正在回复中」状态行；一旦落定仍为空则直接隐藏，避免白块。
    if (assistantIdentity === null || !streaming) return null;
  }

  return (
    <div
      className={cn("group flex w-full flex-col", isUser ? "items-end" : "items-start")}
      data-chat-anchor-key={message.id}
      data-testid="chat-message"
      data-role={message.role}
      onClick={(event) => {
        // v5：@提及点击 → 右栏员工视图（MarkdownRenderer 包裹 span[data-mention-name]）
        if (!onMentionClick) return;
        const node = (event.target as HTMLElement).closest<HTMLElement>("[data-mention-name]");
        const name = node?.dataset.mentionName;
        if (name) onMentionClick(name);
      }}
    >
      <div
        className={cn(
          // 长回复（代码块/表格/多段落）自动切全宽文档布局；其余保持 720px 气泡栏
          documentLayout ? "w-full max-w-full" : "max-w-[min(720px,88%)]",
          // 编辑态：编辑框撑满消息列
          editing && "w-full",
          isUser ? "items-end" : "items-start",
        )}
      >
        {!isUser && !hideIdentity ? (
          message.senderName ? (
            <EmployeeIdentityHeader
              name={assistantLabel}
              metadata={senderEmployee?.metadata}
            />
          ) : (
            <span className="text-chat-muted mb-1 flex items-center gap-1.5 text-sm leading-none">
              <AssistantAvatar className="size-6" />
              {assistantLabel}
            </span>
          )
        ) : null}
        <div
          className={cn(
            // 过程块与气泡统一放内容包裹层（pl-5 与姓名对齐；子元素 w-full 自动收窄
            // 右缘不超出消息列——此前 ml-5 + w-full 会右溢 20px）
            "flex w-full flex-col",
            isUser ? "items-end" : "items-start",
          )}
        >
          {!isUser && message.process?.length ? (
            <ProcessBlock
              nodes={message.process}
              // 消息级总时长（上一条 user 消息 → 本条落卡：
              // 「已完成 · 时间」算卡片落后的总执行时长，而非过程节点累计）
              totalMsOverride={turnDurationMs}
              actions={
                interactive
                  ? {
                      onRetryTool,
                      onSkipTool,
                      onResendTurn,
                      onShowDetail: onToolDetail
                        ? (nodeId) => onToolDetail(message.id, nodeId)
                        : undefined,
                    }
                  : undefined
              }
            />
          ) : null}
          {isUser && message.attachments?.length ? (
            <AttachmentChips
              attachments={message.attachments}
              className="mb-1 justify-end"
              onOpen={onAttachmentOpen}
            />
          ) : null}
          {emptyStreaming ? (
            // 空流式期显示「正在回复中（模型调用中）」状态行——
            // 头像/名字先行；思考/工具过程块或正文一到即覆盖本行
            <span
              data-testid="streaming-status"
              className="text-chat-muted flex items-center gap-1.5 py-1 text-xs"
            >
              <Loader2 className="size-3 animate-spin" />
              {t("chat.composer.responding")}
              {/* 2026-10-01 §4.3：尝试调用阶段显示已等待秒数（opencode StreamingIndicator 同构） */}
              {elapsed > 0 ? (
                <span data-testid="streaming-elapsed" className="tabular-nums">
                  {formatElapsed(elapsed)}
                </span>
              ) : null}
            </span>
          ) : editing ? (
            <div
              className="bg-card border-chat-hairline w-full rounded-xl border px-3 py-2.5"
              data-testid="message-editor"
            >
              <textarea
                data-testid="message-edit-input"
                className="text-chat-ink [field-sizing:content] max-h-60 min-h-10 w-full resize-none bg-transparent text-sm leading-relaxed outline-none"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
              />
              <div className="mt-1 flex items-center gap-2">
                <span className="text-chat-muted flex flex-1 items-center gap-1 text-[11px] leading-4">
                  <Info className="size-3.5 shrink-0" />
                  {t("chat.message.editHint")}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2 text-xs"
                  data-testid="message-edit-cancel"
                  onClick={() => setEditing(false)}
                >
                  {t("common.cancel")}
                </Button>
                <Button
                  size="sm"
                  className="bg-chat-ink hover:bg-chat-ink/90 h-7 px-3 text-xs text-white"
                  data-testid="message-edit-send"
                  disabled={!draft.trim()}
                  onClick={() => {
                    const text = draft.trim();
                    if (!text) return;
                    // 回调返回 false（忙/失败）→ 保持编辑态，防静默丢草稿（-3 修复）
                    void Promise.resolve(onEditResend?.(message.id, text)).then((ok) => {
                      if (ok !== false) setEditing(false);
                    });
                  }}
                >
                  {t("chat.composer.send")}
                </Button>
              </div>
            </div>
          ) : !isUser && !displayContent.trim() ? null : (
            <div
              data-testid="message-bubble"
              className={cn(
                "text-chat-ink rounded-[var(--radius-bubble)] px-3.5 py-2.5 text-sm leading-relaxed",
                // 长回复撑满（左缘 = 头像对齐位、右缘 = 消息列右缘，不溢出）；短消息贴合内容宽
                documentLayout && "w-full max-w-full",
                isUser
                  ? "bg-chat-user-bubble rounded-tr-[4px]"
                  : "bg-card border-chat-hairline rounded-tl-[4px] border",
              )}
            >
              {/* 员工回复：成员任务卡（用户拍板 2026-10-16）——**数据卡优先**（完整
                  任务内容可展开，与右栏计划同源）；无数据卡时正文「## 成员任务」文本段
                  兜底渲染为卡；两者都没有则不渲染。卡片置于其余正文（含风险）之后。 */}
              {isEmployee && memberTaskItems?.length ? (
                <>
                  <MarkdownRenderer
                    text={memberTaskSection.body}
                    plain={isUser}
                    mentions={mentions}
                  />
                  <MemberTaskCards items={memberTaskItems} />
                </>
              ) : isEmployee && memberTaskSection.section ? (
                <>
                  <MarkdownRenderer
                    text={memberTaskSection.body}
                    plain={isUser}
                    mentions={mentions}
                  />
                  <MemberTaskTextCards section={memberTaskSection.section} />
                </>
              ) : (
                <MarkdownRenderer text={displayContent} plain={isUser} mentions={mentions} />
              )}
              {/* 交接行——员工回复底部写明「下一步由谁接手」 */}
              {!isUser && message.nextAssignee ? (
                <p
                  className="border-chat-hairline text-chat-muted mt-1.5 flex flex-wrap items-center gap-x-1 border-t pt-1.5 text-xs"
                  data-testid="next-assignee"
                >
                  <span>{t("chat.message.nextAssignee")}</span>
                  <span className="text-foreground font-medium">
                    {resolveEmployeeName(employees, message.nextAssignee.roleId) ||
                      message.nextAssignee.roleId}
                  </span>
                  {message.nextAssignee.card ? (
                    <span className="text-chat-muted">接手「{message.nextAssignee.card}」</span>
                  ) : null}
                </p>
              ) : null}
              {/* 2026-09-26 用户拍板：移除回复末尾的流式光标（蓝竖条）——逐段出字本身即进度反馈，
                  空内容期由上方「正在回复中…」状态行承担指示 */}
              {/* 2026-10-12 用户拍板：引用文件落在「回复卡片内」最下方，标蓝可点开（裸路径） */}
              {!isUser && replyArtifacts?.length ? (
                <div
                  className="text-chat-muted mt-1.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 border-t border-chat-hairline pt-1.5 text-xs"
                  data-testid="reply-artifacts"
                >
                  <span>{t("chat.memberTask.artifactLabel")}</span>
                  {replyArtifacts.map((artifact) => (
                    <button
                      key={artifact.id}
                      type="button"
                      title={artifact.path}
                      className="text-primary hover:underline max-w-[22rem] truncate underline-offset-2"
                      onClick={() =>
                        usePanelStore
                          .getState()
                          .focusFile(
                            artifact.path,
                            artifact.path.split(/[\\/]/).pop() ?? artifact.path,
                          )
                      }
                    >
                      {artifact.path}
                    </button>
                  ))}
                </div>
              ) : null}
              {!isUser && referencedFiles.length ? (
                <div
                  className="text-chat-muted mt-1.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 border-t border-chat-hairline pt-1.5 text-xs"
                  data-testid="referenced-files"
                >
                  <span>{t("chat.message.referencedFiles")}</span>
                  {referencedFiles.map((path) => (
                    <button
                      key={path}
                      type="button"
                      data-testid="referenced-file-link"
                      title={path}
                      className="text-primary hover:underline max-w-[22rem] truncate underline-offset-2"
                      onClick={() => openReferencedFile(path)}
                    >
                      {path}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          )}
        </div>
        {message.error ? (
          // spec ④：上游中断且重试无果 ⇒ 过程块自动折叠后
          // **弹出重试卡片**。此前只渲染一行红字，重试入口藏在 hover 操作条里
          // （用户看不到）。这里把重试按钮与错误并置，命中「重试无果 → 可重试」语义。
          <div
            data-testid="message-retry-card"
            className="border-destructive/30 bg-destructive/5 mt-1 flex items-center justify-between gap-2 rounded-md border px-2 py-1.5"
          >
            <p className="text-destructive min-w-0 flex-1 text-xs" role="alert">
              {message.error}
            </p>
            {onResendTurn ? (
              <button
                type="button"
                data-testid="message-retry-button"
                className="text-destructive hover:text-destructive/80 shrink-0 text-xs font-medium underline-offset-2 hover:underline"
                onClick={() => onResendTurn()}
              >
                {t("chat.message.resend")}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* 来源链接行（正文/卡片下方、操作条上方；联网检索类回复展示引用链接） */}
      {!editing && !isUser ? (
        <ReplySources
          nodes={message.process}
          timestamp={message.createdAt}
          content={displayContent}
        />
      ) : null}

      {!editing && !isUser ? <CitationChips citations={citations} /> : null}

      {!editing ? (
        <div
          data-testid="message-actions"
          className={cn(
            // user 操作条右对齐（不占满宽——外层 items-end 使其贴气泡右侧）
            "mt-1 flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100",
            // 操作条与卡片左缘对齐
            !interactive && "hidden",
          )}
        >
          {isUser ? (
            <>
              {/* user（2026-09-25 用户拍板）：时间戳贴编辑按钮、不撑开；仅复制 + 编辑 */}
              {message.createdAt ? (
                <span
                  data-testid="message-time"
                  className="text-chat-muted flex h-6 items-center text-xs tabular-nums"
                  title={formatFullTime(message.createdAt)}
                >
                  {formatRelativeTime(message.createdAt)}
                </span>
              ) : null}
              {message.entryId ? (
                <Button
                  variant="ghost"
                  size="icon"
                  className="text-chat-muted size-6"
                  data-testid="message-edit"
                  title={t("chat.message.edit")}
                  onClick={() => {
                    setDraft(message.content);
                    setEditing(true);
                  }}
                >
                  <Pencil className="size-3.5" />
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="icon"
                className="text-chat-muted size-6"
                title={t("common.copy")}
                onClick={copy}
              >
                {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
              </Button>
            </>
          ) : (
            <>
              {/* assistant 顺序：复制、重新生成、点赞、更多，最后时间戳 */}
              <Button
                variant="ghost"
                size="icon"
                className="text-chat-muted size-6"
                title={t("common.copy")}
                onClick={copy}
              >
                {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
              </Button>
              {/* 重新生成：截断到上一条 user 消息之前并重发（SSE 流式由发送链路驱动） */}
              {onRegenerate && message.entryId && !streaming ? (
                <Button
                  variant="ghost"
                  size="icon"
                  className="text-chat-muted size-6"
                  data-testid="message-regenerate"
                  title={t("chat.message.regenerate")}
                  onClick={() => onRegenerate(message.id)}
                >
                  <RefreshCw className="size-3.5" />
                </Button>
              ) : null}
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  "text-chat-muted size-6",
                  message.feedback === "up" && "text-success",
                )}
                title={t("chat.feedback.good")}
                onClick={() => {
                  const next = message.feedback === "up" ? null : "up";
                  const result = onFeedback?.(next);
                  if (result) void result.catch(() => undefined);
                }}
              >
                <ThumbsUp className="size-3.5" />
              </Button>
              {/* 派生/回滚按后端 entry 定位：本地待定稿消息（id 是前端临时键）不提供，防无效请求 */}
              {message.entryId ? (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="text-chat-muted size-6"
                      title={t("common.more")}
                    >
                      <MoreHorizontal className="size-3.5" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="start">
                    <DropdownMenuItem onClick={() => onRevertStage?.(message.entryId as string)}>
                      <RotateCcw /> {t("chat.session.revertToHere")}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : null}
              {message.createdAt ? (
                <span
                  data-testid="message-time"
                  className="text-chat-muted flex h-6 items-center text-xs tabular-nums"
                  title={formatFullTime(message.createdAt)}
                >
                  {formatRelativeTime(message.createdAt)}
                </span>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

/**
 * 消息级 memo——流式增量只重渲染变更的那一条。
 *
 * 父级传入的 `on*` 回调是内联箭头（每次渲染新引用），不参与比较；
 * `assistantIdentity` 是内联对象，按 name/roleId 值比较；其余数据 prop
 * 引用稳定（applyDelta 只为 pending 消息创建新对象）。
 */
function messageItemPropsEqual(prev: MessageItemProps, next: MessageItemProps): boolean {
  const keys = new Set<string>([
    ...Object.keys(prev),
    ...Object.keys(next),
  ]) as Set<keyof MessageItemProps>;
  for (const key of keys) {
    if (typeof key === "string" && key.startsWith("on")) continue;
    if (key === "assistantIdentity") {
      const a = prev.assistantIdentity;
      const b = next.assistantIdentity;
      if ((a === null) !== (b === null)) return false;
      if ((a?.name ?? "") !== (b?.name ?? "") || (a?.roleId ?? "") !== (b?.roleId ?? "")) {
        return false;
      }
      continue;
    }
    if (prev[key] !== next[key]) return false;
  }
  return true;
}

export const MessageItem = memo(MessageItemComponent, messageItemPropsEqual);
