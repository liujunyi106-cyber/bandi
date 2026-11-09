/**
 * 功能：回合中断原因分档（后端 `terminal_reason` → 面向用户的可行动文案）。
 *
 * 背景：中断横幅此前只判断「最后一条是用户消息且回合已结束」，于是
 * **模型不可用 / 上游故障 / 超时 / 安全拦截 / 上下文超长 / 预算耗尽** 六类完全不同的失败
 * 被压成同一句「上一轮回复因网络或模型原因中断了」——既不可行动，也掩盖了真实原因。
 *
 * 分档原则：
 * - 用户能采取的动作不同 → 文案必须不同（换模型 vs 等一会 vs 改短输入 vs 调整预算）；
 * - 拿不到终态（断连，本就没有 `done` 帧）→ 回落原「网络或模型原因」表述；
 * - 成功类终态（COMPLETED / VERIFIED / REWORK）不该出现中断横幅，故归 `none`。
 */

/** 功能：横幅档位——`none` = 不该显示横幅（成功类终态）。 */
export type InterruptionTier =
  | "none"
  | "unknown"
  | "model"
  | "timeout"
  | "safety"
  | "context"
  | "budget"
  | "tools"
  | "loop"
  | "maxTurns";

const TIER_BY_REASON: Record<string, InterruptionTier> = {
  COMPLETED: "none",
  VERIFIED: "none",
  REWORK: "none",
  NO_REPLY: "none",
  AWAITING_APPROVAL: "none",
  USER_INTERRUPT: "none",
  team_routed: "none",
  MODEL_ERROR: "model",
  PROMPT_TOO_LONG: "context",
  SAFETY_BLOCKED: "safety",
  TIMEOUT: "timeout",
  TOKEN_BUDGET_EXHAUSTED: "budget",
  TOOL_ERROR: "tools",
  LOOP_DETECTED: "loop",
  MAX_TURNS: "maxTurns",
  ESCALATED: "model",
};

/** 功能：终态 → 横幅档位；空/未知终态回落 `unknown`（断连等无 `done` 帧的场景）。 */
export function interruptionTier(terminalReason: string | undefined | null): InterruptionTier {
  const reason = (terminalReason ?? "").trim();
  if (!reason) return "none";
  return TIER_BY_REASON[reason] ?? "unknown";
}

/** 功能：横幅提示文案 i18n key（按档位；`unknown` 沿用原「网络或模型」口径）。 */
export function interruptionHintKey(tier: InterruptionTier): string {
  switch (tier) {
    case "model":
      return "chat.interrupt.model";
    case "timeout":
      return "chat.interrupt.timeout";
    case "safety":
      return "chat.interrupt.safety";
    case "context":
      return "chat.interrupt.context";
    case "budget":
      return "chat.interrupt.budget";
    case "tools":
      return "chat.interrupt.tools";
    case "loop":
      return "chat.interrupt.loop";
    case "maxTurns":
      return "chat.interrupt.maxTurns";
    default:
      return "chat.interrupt.unknown";
  }
}

/** 功能：该档位下「重试」是否有意义（预算耗尽/上下文超长重试必然再失败 ⇒ 换引导文案）。 */
export function tierAllowsRetry(tier: InterruptionTier): boolean {
  return tier !== "budget" && tier !== "context" && tier !== "maxTurns";
}