/**
 * 功能：会话活动态聚合测试——项目行/头像行的状态来源。
 *
 * 破坏者视角：running 必须优先于 done（有在跑的在，就不能显示成"已完成"）；
 * 当前会话必须被跳过（用户正看着它，不该再显示提醒）。
 */
import { describe, expect, it } from "vitest";

import {
  aggregateDmActivity,
  aggregateSessionActivity,
  shouldClearOnOpen,
} from "@/lib/session-activity";

describe("aggregateSessionActivity", () => {
  it("无记录 → 空", () => {
    expect(aggregateSessionActivity(["a", "b"], {})).toBe("");
  });

  it("任一会话 done → done", () => {
    expect(aggregateSessionActivity(["a", "b"], { b: "done" })).toBe("done");
  });

  it("任一会话 running → running（优先于 done）", () => {
    expect(aggregateSessionActivity(["a", "b"], { a: "done", b: "running" })).toBe("running");
  });

  it("跳过当前会话：其状态不参与聚合", () => {
    // 当前会话在跑 → 不显示；组内其他会话无事 → 空
    expect(aggregateSessionActivity(["a", "b"], { a: "running" }, "a")).toBe("");
    // 当前会话 done 被跳过，但另一条在跑 → running
    expect(aggregateSessionActivity(["a", "b"], { a: "done", b: "running" }, "a")).toBe("running");
  });

  it("空列表 → 空（不抛错）", () => {
    expect(aggregateSessionActivity([], { a: "running" })).toBe("");
  });
});

describe("shouldClearOnOpen", () => {
  it("done 才清除（打开会话=已读）", () => {
    expect(shouldClearOnOpen("done")).toBe(true);
  });

  it("running 不清除（在跑就得继续显示）", () => {
    expect(shouldClearOnOpen("running")).toBe(false);
  });

  it("无状态不为真（避免无谓写）", () => {
    expect(shouldClearOnOpen(undefined)).toBe(false);
  });
});

describe("aggregateDmActivity（员工单聊，线程制无 session id）", () => {
  it("该员工任一在跑 → running", () => {
    expect(aggregateDmActivity("p1", "dev", { "p1::t1::dev": { length: 2 } }, {})).toBe("running");
  });

  it("已完成的空线程 → done", () => {
    expect(aggregateDmActivity("p1", "dev", {}, { "p1::t1::dev": true })).toBe("done");
  });

  it("running 优先于 done（多线程并存）", () => {
    expect(
      aggregateDmActivity("p1", "dev", { "p1::t2::dev": { length: 1 } }, { "p1::t1::dev": true }),
    ).toBe("running");
  });

  it("他项目 / 他员工的键不得误命中", () => {
    expect(
      aggregateDmActivity("p1", "dev", { "p2::t1::dev": { length: 3 } }, { "p1::t1::ops": true }),
    ).toBe("");
  });

  it("空线程且无完成标记 → 空", () => {
    expect(aggregateDmActivity("p1", "dev", { "p1::t1::dev": { length: 0 } }, {})).toBe("");
  });
});
