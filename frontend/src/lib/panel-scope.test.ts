/**
 * 功能：右栏面板作用域测试（.2）——会话（session）与项目内员工单聊
  * （employee:<项目>::<角色>）两形态的键解析（）。
 */
import { describe, expect, it } from "vitest";

import { employeePanelScope, resolvePanelScope, sessionPanelScope } from "@/lib/panel-scope";

describe("panel-scope", () => {
  it("会话作用域：session:<id>；空会话无作用域", () => {
    expect(sessionPanelScope("s1")?.key).toBe("session:s1");
    expect(sessionPanelScope(" s1 ")?.key).toBe("session:s1");
    expect(sessionPanelScope("")).toBeNull();
    expect(sessionPanelScope("   ")).toBeNull();
  });

  it("员工作用域：employee:<项目>::<角色>；角色缺失无作用域", () => {
    const scope = employeePanelScope("p1", "dev");
    expect(scope?.kind).toBe("employee");
    expect(scope?.key).toBe("employee:p1::dev");
    if (scope?.kind === "employee") {
      expect(scope.projectId).toBe("p1");
      expect(scope.roleId).toBe("dev");
    }
    expect(employeePanelScope("p1", "")).toBeNull();
  });

  it("路由解析：DM 路由 → 员工作用域（URL project 优先，缺省回落当前项目）", () => {
    const dm = resolvePanelScope({
      search: "?view=dm&target=dev&project=p1",
      sessionId: "s1",
      projectId: "p2",
    });
    expect(dm?.kind).toBe("employee");
    expect(dm?.key).toBe("employee:p1::dev");

    const fallback = resolvePanelScope({
      search: "?view=dm&target=dev",
      sessionId: "s1",
      projectId: "p2",
    });
    expect(fallback?.key).toBe("employee:p2::dev");
  });

  it("路由解析：非 DM 路由 → 会话作用域；无会话为空", () => {
    const session = resolvePanelScope({ search: "", sessionId: "s1", projectId: "" });
    expect(session?.kind).toBe("session");
    expect(session?.key).toBe("session:s1");

    const projectRoute = resolvePanelScope({
      search: "?project=p1",
      sessionId: "",
      projectId: "p1",
    });
    expect(projectRoute).toBeNull();
  });

  it("破坏者：DM 无 target 不误判为员工作用域、也不回落他会话", () => {
    expect(resolvePanelScope({ search: "?view=dm", sessionId: "s1", projectId: "p1" })).toBeNull();
    expect(
      resolvePanelScope({ search: "?view=dm&target=", sessionId: "s1", projectId: "" }),
    ).toBeNull();
    expect(
      resolvePanelScope({ search: "?target=dev", sessionId: "s1", projectId: "p1" })?.kind,
    ).toBe("session");
  });
});
