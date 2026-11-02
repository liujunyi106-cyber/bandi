/**
 * 契约：中断横幅档位——`team_routed`（团队接管移交成功）必须归 none，
  * 否则每次群聊发送都会误显示「重试横幅」。
 */
import { describe, expect, it } from "vitest";

import { interruptionHintKey, interruptionTier, tierAllowsRetry } from "./interruption";

describe("interruptionTier", () => {
  it("team_routed（团队移交成功）不显示中断横幅且无重试", () => {
    expect(interruptionTier("team_routed")).toBe("none");
    expect(tierAllowsRetry(interruptionTier("team_routed"))).toBe(true);
    // none 档不渲染横幅，文案取 key 仅为穷尽性
    expect(interruptionHintKey(interruptionTier("team_routed"))).toBe("chat.interrupt.unknown");
  });

  it("成功类终态归 none", () => {
    for (const reason of ["COMPLETED", "VERIFIED", "REWORK", "NO_REPLY", "USER_INTERRUPT"]) {
      expect(interruptionTier(reason)).toBe("none");
    }
  });

  it("失败类终态分档不回归", () => {
    expect(interruptionTier("MODEL_ERROR")).toBe("model");
    expect(interruptionTier("TIMEOUT")).toBe("timeout");
    expect(interruptionTier("TOKEN_BUDGET_EXHAUSTED")).toBe("budget");
  });

  it("空终态不误报中断（历史会话无终态字段 / 取消后清空）", () => {
    expect(interruptionTier("")).toBe("none");
    expect(interruptionTier(undefined)).toBe("none");
  });
});
