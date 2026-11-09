/**
 * 功能：中断原因分档 + 记忆写入通道分档。
 *
 * ① 中断提示按 `terminal_reason` 分档：模型不可用 / 超时 / 安全拦截 / 上下文超长 /
 *    预算耗尽 各给可行动文案，不再一律「因网络或模型原因中断了」；
 * ② 成功类终态不该出现中断横幅；
 * ③ 拿不到终态（断连无 `done` 帧）回落原口径；
 * ④ 预算/上下文/步数上限三档不给「重试」（重试必然再失败）。
 * ⑤ 记忆写入行按通道分档：`working`/`compaction` 是草稿与纪要，不混称「记忆」。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProcessBlock } from "@/components/chat/ProcessBlock";
import {
  interruptionHintKey,
  interruptionTier,
  tierAllowsRetry,
} from "@/lib/interruption";
import { useChatStore, type ProcessNode } from "@/stores/chat-store";

vi.mock("@/hooks/use-employees", () => ({
  useEmployees: () => ({ employees: [], loading: false, error: null }),
}));

describe("① 中断原因分档", () => {
  it("六类失败各归各档（用户能采取的动作不同 ⇒ 文案必须不同）", () => {
    expect(interruptionTier("MODEL_ERROR")).toBe("model");
    expect(interruptionTier("TIMEOUT")).toBe("timeout");
    expect(interruptionTier("SAFETY_BLOCKED")).toBe("safety");
    expect(interruptionTier("PROMPT_TOO_LONG")).toBe("context");
    expect(interruptionTier("TOKEN_BUDGET_EXHAUSTED")).toBe("budget");
    expect(interruptionTier("TOOL_ERROR")).toBe("tools");
    expect(interruptionTier("LOOP_DETECTED")).toBe("loop");
    expect(interruptionTier("MAX_TURNS")).toBe("maxTurns");
  });

  it("每档文案 key 互不相同（防「分档了但都指向同一句」）", () => {
    const tiers = ["model", "timeout", "safety", "context", "budget", "tools", "loop", "maxTurns"];
    const keys = tiers.map((tier) => interruptionHintKey(tier as never));
    expect(new Set(keys).size).toBe(tiers.length);
  });
});

describe("② 成功类终态不该出现中断横幅", () => {
  it("COMPLETED/VERIFIED/REWORK/NO_REPLY/待审批/用户中断 → none", () => {
    for (const reason of [
      "COMPLETED",
      "VERIFIED",
      "REWORK",
      "NO_REPLY",
      "AWAITING_APPROVAL",
      "USER_INTERRUPT",
    ]) {
      expect(interruptionTier(reason), reason).toBe("none");
    }
  });
});

describe("③ 断连场景回落原口径", () => {
  it("空终态 → none（不误报）；未知原因 → unknown", () => {
    expect(interruptionTier("")).toBe("none");
    expect(interruptionTier(undefined)).toBe("none");
    expect(interruptionTier(null)).toBe("none");
    expect(interruptionTier("SOME_FUTURE_REASON")).toBe("unknown");
    expect(interruptionHintKey("unknown")).toBe("chat.interrupt.unknown");
  });
});

describe("④ 重试按钮按档位取舍", () => {
  it("预算耗尽/上下文超长/步数上限不给重试（重试必然再失败）", () => {
    expect(tierAllowsRetry("budget")).toBe(false);
    expect(tierAllowsRetry("context")).toBe(false);
    expect(tierAllowsRetry("maxTurns")).toBe(false);
  });

  it("模型/超时/安全拦截等可重试", () => {
    expect(tierAllowsRetry("model")).toBe(true);
    expect(tierAllowsRetry("timeout")).toBe(true);
    expect(tierAllowsRetry("safety")).toBe(true);
    expect(tierAllowsRetry("unknown")).toBe(true);
  });
});

describe("⑤ 终态写入 store（供横幅读）", () => {
  beforeEach(() => {
    useChatStore.setState({ sessionTerminals: {} });
  });

  it("markSessionTerminal 按会话记录终态原值", () => {
    useChatStore.getState().markSessionTerminal("s1", "MODEL_ERROR");
    useChatStore.getState().markSessionTerminal("s2", "TIMEOUT");
    const state = useChatStore.getState().sessionTerminals;
    expect(state.s1).toBe("MODEL_ERROR");
    expect(state.s2).toBe("TIMEOUT");
    expect(interruptionTier(state.s1)).toBe("model");
    expect(interruptionTier(state.s2)).toBe("timeout");
  });
});

describe("⑥ 记忆写入行：按真实通道分档、长期在上草稿在下", () => {
  function memoryNote(count: number, channels?: string[]): ProcessNode {
    return {
      id: "n1",
      kind: "note",
      noteType: "memory_written",
      count,
      channels,
      status: "success",
    } as ProcessNode;
  }

  function renderNote(note: ProcessNode) {
    // 记忆写入行是「过程块内注记」——纯 note 块不渲染明细，附一个工具节点同既有形态一致
    render(
      <ProcessBlock
        nodes={[
          { id: "t1", kind: "tool", toolName: "bash", input: {}, status: "success", detail: "", durationMs: 5 },
          note,
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("process-toggle"));
  }

  function tierLabels(): string[] {
    return screen.queryAllByTestId("process-memory-tier").map((el) => el.textContent ?? "");
  }

  it("按写入条数显示「计入记忆 xN」", () => {
    renderNote(memoryNote(2));
    expect(screen.getByText("计入记忆 x2")).toBeInTheDocument();
  });

  it("每档独占一行：长期 → 草稿", () => {
    renderNote(memoryNote(3, ["long_term", "working", "compaction"]));
    expect(tierLabels()).toEqual(["长期记忆 x1", "本轮草稿 x2"]);
  });

  it("episodic 通道不产生任何行（情景记忆不进过程块）", () => {
    renderNote(memoryNote(2, ["episodic", "long_term"]));
    expect(tierLabels()).toEqual(["长期记忆 x1"]);
    expect(screen.queryByText(/情景记忆/)).not.toBeInTheDocument();
  });

  it("只有 episodic 时整块不显示记忆行", () => {
    renderNote(memoryNote(1, ["episodic"]));
    expect(tierLabels()).toEqual([]);
    expect(screen.queryByText(/情景记忆/)).not.toBeInTheDocument();
    expect(screen.queryByText(/长期记忆/)).not.toBeInTheDocument();
  });

  it("长期记忆行排在草稿行之前", () => {
    renderNote(memoryNote(2, ["working", "long_term"]));
    const tiers = screen.getAllByTestId("process-memory-tier");
    expect(tiers.map((el) => el.getAttribute("data-tier"))).toEqual(["durable", "scratch"]);
  });

  it("每行带图标", () => {
    renderNote(memoryNote(1, ["long_term"]));
    const row = screen.getByTestId("process-memory-tier");
    expect(row.querySelector("svg")).not.toBeNull();
  });

  it("缺 count 时兜底 x0，不渲染空文案", () => {
    renderNote({ id: "n1", kind: "note", noteType: "memory_written", status: "success" } as ProcessNode);
    expect(screen.getByText("计入记忆 x0")).toBeInTheDocument();
  });
});