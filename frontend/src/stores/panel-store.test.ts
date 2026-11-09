/**
 * 功能：右栏 v5 状态机测试。
 */
import { beforeEach, describe, expect, it } from "vitest";

import { panelViewsForScope } from "@/lib/panel-scope";
import { migratePanelPersisted, PANEL_WIDTH_DEFAULT, usePanelStore } from "@/stores/panel-store";

function reset() {
  usePanelStore.setState({
    open: false,
    fullscreen: false,
    view: "overview",
    tabs: [],
    activeTabId: "",
    fileFocusPath: "",
    expandRoleId: "",
    toolFocus: null,
    taskFocusId: "",
    dismissedAuto: {},
    autoOpened: {},
    widthBySession: {},
  });
}

describe("panel-store", () => {
  beforeEach(reset);

  it("openTab 去重（同 id 只激活不重复添加）+ closeTab 相邻激活", () => {
    const store = usePanelStore.getState();
    store.openTab({ kind: "file", title: "a.md", path: "a.md" });
    store.openTab({ kind: "file", title: "b.md", path: "b.md" });
    store.openTab({ kind: "file", title: "a.md", path: "a.md" });
    expect(usePanelStore.getState().tabs).toHaveLength(2);
    expect(usePanelStore.getState().activeTabId).toBe("file:a.md");

    usePanelStore.getState().closeTab("file:a.md");
    expect(usePanelStore.getState().tabs.map((tab) => tab.id)).toEqual(["file:b.md"]);
    expect(usePanelStore.getState().activeTabId).toBe("file:b.md");

    usePanelStore.getState().closeTab("file:b.md");
    expect(usePanelStore.getState().activeTabId).toBe("");
  });

  it("页签上限 8：超出淘汰最早的非激活页签", () => {
    const store = usePanelStore.getState();
    for (let index = 0; index < 9; index += 1) {
      store.openTab({ kind: "file", title: `f${index}.md`, path: `f${index}.md` });
    }
    const tabs = usePanelStore.getState().tabs;
    expect(tabs).toHaveLength(8);
    expect(tabs.map((tab) => tab.id)).not.toContain("file:f0.md");
    expect(tabs.map((tab) => tab.id)).toContain("file:f8.md");
  });

  it("破坏者：closeTab 非激活页签不动 activeTabId", () => {
    const store = usePanelStore.getState();
    store.openTab({ kind: "file", title: "a.md", path: "a.md" });
    store.openTab({ kind: "file", title: "b.md", path: "b.md" });
    usePanelStore.getState().closeTab("file:a.md");
    expect(usePanelStore.getState().activeTabId).toBe("file:b.md");
    expect(usePanelStore.getState().tabs).toHaveLength(1);
  });

  it("联动：focusFile 打开文件页签 + 文件视图 + 定位；focusTool/focusTask/focusEmployee 各归其视图", () => {
    usePanelStore.getState().focusFile("docs/out.md");
    expect(usePanelStore.getState().open).toBe(true);
    expect(usePanelStore.getState().view).toBe("files");
    expect(usePanelStore.getState().fileFocusPath).toBe("docs/out.md");
    expect(usePanelStore.getState().activeTabId).toBe("file:docs/out.md");

    usePanelStore.getState().focusTool("m1", "n1");
    expect(usePanelStore.getState().view).toBe("details");
    expect(usePanelStore.getState().toolFocus).toEqual({ messageId: "m1", nodeId: "n1" });
    usePanelStore.getState().clearToolFocus();
    expect(usePanelStore.getState().toolFocus).toBeNull();

    usePanelStore.getState().focusTask("t1");
    expect(usePanelStore.getState().view).toBe("overview");
    expect(usePanelStore.getState().taskFocusId).toBe("t1");

    usePanelStore.getState().focusEmployee("dev");
    expect(usePanelStore.getState().view).toBe("employees");
    expect(usePanelStore.getState().expandRoleId).toBe("dev");

    usePanelStore.getState().focusArtifact("a1", "out.md", "docs/out.md");
    expect(usePanelStore.getState().activeTabId).toBe("artifact:a1");
    expect(usePanelStore.getState().fileFocusPath).toBe("docs/out.md");
  });

  it("自动展开：首个活动只开一次；用户关过不再弹（按会话记忆）", () => {
    usePanelStore.getState().maybeAutoOpen("s1");
    expect(usePanelStore.getState().open).toBe(true);
    usePanelStore.getState().setOpen(false);
    usePanelStore.getState().maybeAutoOpen("s1");
    expect(usePanelStore.getState().open).toBe(false);

    usePanelStore.getState().maybeAutoOpen("s2");
    expect(usePanelStore.getState().open).toBe(true);
    usePanelStore.getState().collapseByUser("s2");
    expect(usePanelStore.getState().open).toBe(false);
    expect(usePanelStore.getState().dismissedAuto.s2).toBe(true);
    usePanelStore.getState().maybeAutoOpen("s2");
    expect(usePanelStore.getState().open).toBe(false);
  });

  it("全屏：setOpen(false)/collapseByUser 强制退出全屏（防布局留白）", () => {
    usePanelStore.getState().setOpen(true);
    usePanelStore.getState().toggleFullscreen();
    expect(usePanelStore.getState().fullscreen).toBe(true);
    usePanelStore.getState().setOpen(false);
    expect(usePanelStore.getState().fullscreen).toBe(false);
  });

  it("resetForSession 清运行时态（页签/联动选中），保留开合与视图", () => {
    usePanelStore.getState().setOpen(true);
    usePanelStore.getState().focusTool("m1", "n1");
    usePanelStore.getState().setView("files");
    usePanelStore.getState().focusFile("a.md");
    usePanelStore.getState().resetForSession();
    expect(usePanelStore.getState().tabs).toHaveLength(0);
    expect(usePanelStore.getState().activeTabId).toBe("");
    expect(usePanelStore.getState().toolFocus).toBeNull();
    expect(usePanelStore.getState().fileFocusPath).toBe("");
    expect(usePanelStore.getState().open).toBe(true);
    expect(usePanelStore.getState().view).toBe("files");
  });

  it("setWidth 按会话钳制（260~640；2026-10-03 min 两轮下调 320→280→260）", () => {
    usePanelStore.getState().setWidth("s1", 100);
    expect(usePanelStore.getState().widthBySession.s1).toBe(260);
    usePanelStore.getState().setWidth("s1", 9999);
    expect(usePanelStore.getState().widthBySession.s1).toBe(640);
    usePanelStore.getState().setWidth("s1", PANEL_WIDTH_DEFAULT + 10);
    expect(usePanelStore.getState().widthBySession.s1).toBe(PANEL_WIDTH_DEFAULT + 10);
  });

  it("resetWidth 清除手动覆盖（2026-10-04：双击手柄回窗口自适应）", () => {
    usePanelStore.getState().setWidth("s1", 420);
    expect(usePanelStore.getState().widthBySession.s1).toBe(420);
    usePanelStore.getState().resetWidth("s1");
    expect(usePanelStore.getState().widthBySession.s1).toBeUndefined();
    // 破坏者：清不存在的键不炸、不动其他作用域
    usePanelStore.getState().setWidth("s2", 300);
    usePanelStore.getState().resetWidth("none");
    expect(usePanelStore.getState().widthBySession).toEqual({ s2: 300 });
  });
});

describe("panelViewsForScope（B1.2 视图集）", () => {
  it("会话作用域 = 四视图全量", () => {
    expect(panelViewsForScope({ kind: "session", key: "session:s1", sessionId: "s1" })).toEqual([
      "overview",
      "employees",
      "files",
      "details",
    ]);
  });

  it("员工作用域 = 概览/文件（无员工/详情）", () => {
    expect(
      panelViewsForScope({
        kind: "employee",
        key: "employee:p1::dev",
        projectId: "p1",
        roleId: "dev",
      }),
    ).toEqual(["overview", "files"]);
  });

  it("破坏者：无作用域回落会话视图集（不因缺省误删视图）", () => {
    expect(panelViewsForScope(null)).toContain("details");
    expect(panelViewsForScope(null)).toHaveLength(4);
  });
});

describe("migratePanelPersisted（v1→v2 作用域键前缀；v2→v3 宽度语义改手动覆盖）", () => {
  it("v1 旧裸会话键迁移为 session: 前缀；宽度固定值 v3 作废清空", () => {
    const migrated = migratePanelPersisted(
      {
        open: true,
        view: "files",
        widthBySession: { s1: 420 },
        dismissedAuto: { s1: true },
        autoOpened: { s1: true },
      },
      1,
    ) as Record<string, unknown>;
    // 宽度改「手动覆盖」模型——旧固定值作废（回窗口自适应）
    expect(migrated.widthBySession).toEqual({});
    expect(migrated.dismissedAuto).toEqual({ "session:s1": true });
    expect(migrated.autoOpened).toEqual({ "session:s1": true });
    expect(migrated.view).toBe("files");
    expect(migrated.open).toBe(true);
  });

  it("v2→v3 宽度清空；v3 数据不迁移；非对象安全回落", () => {
    const v2 = migratePanelPersisted({ widthBySession: { s1: 1 } }, 2) as Record<string, unknown>;
    expect(v2.widthBySession).toEqual({});

    const v3 = migratePanelPersisted({ widthBySession: { "session:s1": 340 } }, 3) as Record<
      string,
      unknown
    >;
    expect(v3.widthBySession).toEqual({ "session:s1": 340 });

    expect(migratePanelPersisted(null, 1)).toBeNull();
    expect(migratePanelPersisted("bad", 1)).toBe("bad");
  });
});
