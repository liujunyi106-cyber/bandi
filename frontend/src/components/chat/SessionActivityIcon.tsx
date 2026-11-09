/**
 * 功能：条目任务状态指示（执行中转圈 / 已完成灰点）——会话行、项目行、面板头像行共用。
 *
 * 状态语义见 `lib/session-activity.ts`（running 优先于 done）；本组件只负责呈现，
 * 位置与避让由调用方的槽位容器控制（会话行/项目行的槽位在悬浮时右移让位给「更多」按钮）。
 */
import { Loader2 } from "lucide-react";

import { useT } from "@/i18n";

import type { ActivityState } from "@/lib/session-activity";

interface SessionActivityIconProps {
  /** 聚合后的状态；空串 = 不渲染（避免占位） */
  state: ActivityState;
  /** testid 前缀——产物为 `${testId}-running` / `${testId}-done` */
  testId: string;
}

export function SessionActivityIcon({ state, testId }: SessionActivityIconProps) {
  const t = useT();
  if (state === "running") {
    return (
      <Loader2
        data-testid={`${testId}-running`}
        className="text-primary block size-3 animate-spin"
      />
    );
  }
  if (state === "done") {
    return (
      <span
        data-testid={`${testId}-done`}
        title={t("chat.session.repliedWhileAway")}
        className="text-chat-muted/70 block size-1.5 rounded-full bg-current"
      />
    );
  }
  return null;
}
