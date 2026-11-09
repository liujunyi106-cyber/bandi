import { describe, expect, it } from "vitest";

import {
  groupSessionsByEmployee,
  isEmployeeDmSession,
  type SessionSummary,
} from "@/services/sessions";

function session(overrides: Partial<SessionSummary>): SessionSummary {
  return {
    id: "s1",
    title: "会话",
    created_at: "2026-10-28T00:00:00",
    message_count: 1,
    project_id: "",
    ...overrides,
  };
}

describe("员工单聊会话分组", () => {
  it("按 target_role 分组并排除普通/群聊会话", () => {
    const groups = groupSessionsByEmployee([
      session({ id: "a", target_role: "lead", origin: "employee_dm" }),
      session({ id: "b", target_role: "lead", origin: "employee_dm" }),
      session({ id: "c", target_role: "worker", origin: "employee_dm" }),
      session({ id: "d", origin: "secretary" }),
      session({ id: "e", origin: "" }),
    ]);
    expect(Array.from(groups.keys())).toEqual(["lead", "worker"]);
    expect(groups.get("lead")?.map((item) => item.id)).toEqual(["a", "b"]);
  });

  it("origin 或 target_role 任一命中即员工单聊", () => {
    expect(isEmployeeDmSession(session({ origin: "employee_dm" }))).toBe(true);
    expect(isEmployeeDmSession(session({ target_role: "lead" }))).toBe(true);
    expect(isEmployeeDmSession(session({ origin: "secretary" }))).toBe(false);
    expect(isEmployeeDmSession(session({}))).toBe(false);
  });
});
