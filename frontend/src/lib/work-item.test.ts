import { describe, expect, it } from "vitest";

import {
  actionsOf,
  columnOfPhase,
  filterItems,
  formatTime,
  groupByColumn,
  hasAnyAction,
  sortByUpdated,
} from "@/lib/work-item";
import type { WorkItemSummary } from "@/services/company";

function item(id: string, phase: string, extra: Partial<WorkItemSummary> = {}): WorkItemSummary {
  return {
    id,
    title: `任务 ${id}`,
    phase,
    assignee_role: "产品设计师",
    updated_at: "2026-10-27T09:00:00+00:00",
    ...extra,
  };
}

describe("columnOfPhase", () => {
  it("草稿归待办（没在跑就不该占进行中）", () => {
    expect(columnOfPhase("paused")).toBe("todo");
  });

  it("失败 / 取消归已完成（靠徽标区分，不单独占列）", () => {
    expect(columnOfPhase("failed")).toBe("done");
    expect(columnOfPhase("cancelled")).toBe("done");
  });

  it("执行与等待态归进行中", () => {
    for (const phase of ["running", "waiting_for_peer", "waiting_for_children", "needs_attention"]) {
      expect(columnOfPhase(phase)).toBe("in_progress");
    }
  });

  it("评审态归待审，通过归已完成", () => {
    expect(columnOfPhase("awaiting_human")).toBe("in_review");
    expect(columnOfPhase("awaiting_manager_review")).toBe("in_review");
    expect(columnOfPhase("approved")).toBe("done");
  });

  it("未知阶段兜底归已完成（不丢卡）", () => {
    expect(columnOfPhase("something_new")).toBe("done");
  });
});

describe("groupByColumn", () => {
  it("四列都返回，空列也要能画空态", () => {
    const grouped = groupByColumn([item("a", "queued")]);
    expect(Object.keys(grouped)).toEqual(["todo", "in_progress", "in_review", "done"]);
    expect(grouped.todo).toHaveLength(1);
    expect(grouped.done).toHaveLength(0);
  });

  it("草稿与失败各落一列", () => {
    const grouped = groupByColumn([item("a", "paused"), item("b", "failed")]);
    expect(grouped.todo.map((entry) => entry.id)).toEqual(["a"]);
    expect(grouped.done.map((entry) => entry.id)).toEqual(["b"]);
  });
});

describe("actionsOf", () => {
  it("排队卡没有任何动作——计划卡不再需要人工放行", () => {
    const gated = actionsOf({ phase: "queued" });
    expect(hasAnyAction(gated)).toBe(false);
  });

  it("已暂停的历史卡没有任何动作（草稿态已废弃）", () => {
    const actions = actionsOf({ phase: "paused" });
    expect(hasAnyAction(actions)).toBe(false);
  });

  it("需关注只能重试", () => {
    const actions = actionsOf({ phase: "needs_attention" });
    expect(actions.canRetry).toBe(true);
    expect(actions.canReview || actions.canAbort).toBe(false);
  });

  it("待审可评审，执行中可中止", () => {
    expect(actionsOf({ phase: "awaiting_human" }).canReview).toBe(true);
    expect(actionsOf({ phase: "awaiting_manager_review" }).canReview).toBe(true);
    expect(actionsOf({ phase: "running" }).canAbort).toBe(true);
  });

  it("破坏者：失败卡不给任何动作（重试接口只接受需关注）", () => {
    const actions = actionsOf({ phase: "failed" });
    expect(hasAnyAction(actions)).toBe(false);
  });
});

describe("formatTime 时区口径", () => {
  it("带时区偏移：按偏移换算（与同一时刻的时间对象结果一致）", () => {
    // 期望值从同一时刻推导，避免把运行机器的时区写死进断例
    const instant = new Date(Date.UTC(2026, 9, 27, 15, 35, 12));
    expect(formatTime(instant.toISOString())).toBe(formatTime(instant));
    expect(formatTime("2026-10-27 15:35:12+00:00")).toBe(formatTime(instant));
  });

  it("破坏者：缺时区偏移（历史本地 naive / 空格分隔）按本地读，不得当成 UTC", () => {
    const moment = new Date(2026, 9, 27, 23, 35, 12);
    expect(formatTime("2026-10-27 23:35:12")).toBe(formatTime(moment));
    expect(formatTime("2026-10-27T23:35:12")).toBe(formatTime(moment));
  });

  it("解析失败返回空串（不显示 Invalid Date）", () => {
    expect(formatTime("not-a-time")).toBe("");
    expect(formatTime("")).toBe("");
  });
});

describe("filterItems / sortByUpdated", () => {
  const items = [
    item("ai-design-team_spec", "queued", { title: "需求规格", assignee_role: "产品设计师" }),
    item("ai-design-team_ui", "paused", { title: "视觉稿", assignee_role: "视觉设计师" }),
  ];

  it("按岗位与关键词过滤", () => {
    expect(filterItems(items, { role: "视觉设计师" }).map((entry) => entry.id)).toEqual([
      "ai-design-team_ui",
    ]);
    expect(filterItems(items, { query: "需求" }).map((entry) => entry.id)).toEqual([
      "ai-design-team_spec",
    ]);
  });

  it("按团队角色集合过滤", () => {
    expect(
      filterItems(items, {
        teamRoleIds: new Set(["视觉设计师", "其他角色"]),
      }).map((entry) => entry.id),
    ).toEqual(["ai-design-team_ui"]);
    expect(filterItems(items, { teamRoleIds: new Set() })).toHaveLength(2);
  });

  it("按更新时间倒序（同刻按 id 稳定）", () => {
    const sorted = sortByUpdated([
      item("b", "queued", { updated_at: "2026-10-27T09:00:00+00:00" }),
      item("a", "queued", { updated_at: "2026-10-27T10:00:00+00:00" }),
    ]);
    expect(sorted.map((entry) => entry.id)).toEqual(["a", "b"]);
  });
});
