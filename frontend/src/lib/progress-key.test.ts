/**
 * 功能：过程槽键口径单测（项目口径 + 线程维度）——
 * 写侧/读侧必须同口径；空值不得让「写进去的键」与「读的键」错开；
 * 不同线程（= 不同次执行）必须是不同槽。
 */
import { describe, expect, it } from "vitest";

import { DEFAULT_PROJECT_SCOPE, normalizeProjectScope, progressKey } from "@/lib/progress-key";

describe("progress-key（REV-137b2-6 / -11）", () => {
  it("键形状 = 项目::线程::员工（正常值原样保留）", () => {
    expect(normalizeProjectScope("proj-a")).toBe("proj-a");
    expect(progressKey("proj-a", "t9", "r1")).toBe("proj-a::t9::r1");
  });

  it("破坏者：空/空白/未定义项目 → 统一回退，两侧拼出的键必须一致", () => {
    expect(normalizeProjectScope("")).toBe(DEFAULT_PROJECT_SCOPE);
    expect(normalizeProjectScope("   ")).toBe(DEFAULT_PROJECT_SCOPE);
    expect(normalizeProjectScope(undefined)).toBe(DEFAULT_PROJECT_SCOPE);
    expect(normalizeProjectScope(null)).toBe(DEFAULT_PROJECT_SCOPE);
    // 写侧拿空值、读侧拿 "company"（或反之）→ 必须是同一个键
    expect(progressKey("", "t1", "r1")).toBe(progressKey("company", "t1", "r1"));
    expect(progressKey(undefined, "t1", "r1")).toBe(progressKey("company", "t1", "r1"));
  });

  it("两侧带空白也归一（避免「  p1」与「p1」分家）", () => {
    expect(progressKey(" p1 ", "t1", "r1")).toBe(progressKey("p1", "t1", "r1"));
  });

  it("破坏者（-11）：同员工、不同线程 → 必须落到不同槽", () => {
    expect(progressKey("p1", "tA", "r1")).not.toBe(progressKey("p1", "tB", "r1"));
  });

  it("线程缺省（undefined/null）与空串等价（工作项无 thread 时不产回复，槽无人消费）", () => {
    expect(progressKey("p1", undefined, "r1")).toBe("p1::::r1");
    expect(progressKey("p1", null, "r1")).toBe(progressKey("p1", "", "r1"));
  });
});
