import { describe, expect, it } from "vitest";

import { finalizeMessage } from "@/stores/chat-store";
import type { ChatMessage } from "@/stores/chat-store";

/** 功能：构造一条流式中的 assistant 消息。 */
function pending(id = "p1"): ChatMessage {
  return {
    id,
    sessionId: "s1",
    role: "assistant",
    content: "回复正文",
    done: false,
    createdAt: "2026-11-13T10:00:00.000Z",
  };
}

describe("已完成 · Ns 的计时口径", () => {
  it("done 帧携带 duration_ms 时按真实执行耗时落存", () => {
    const [msg] = finalizeMessage([pending()], "p1", { turnDurationMs: 123_400 });
    expect(msg.done).toBe(true);
    expect(msg.turnDurationMs).toBe(123_400);
  });

  it("缺失 duration_ms 时不编造数值（交给过程块回退）", () => {
    const [msg] = finalizeMessage([pending()], "p1", {});
    expect(msg.done).toBe(true);
    expect(msg.turnDurationMs).toBeUndefined();
  });

  it("duration_ms 为 0（后端未记录）时不写入零值", () => {
    const [msg] = finalizeMessage([pending()], "p1", { turnDurationMs: 0 });
    expect(msg.turnDurationMs).toBe(0);
  });

  it("不触碰非目标消息", () => {
    const other: ChatMessage = { ...pending("p2"), content: "另一条", done: true };
    const out = finalizeMessage([pending(), other], "p1", { turnDurationMs: 9000 });
    expect(out[1]).toBe(other);
    expect(out[0].turnDurationMs).toBe(9000);
  });
});
