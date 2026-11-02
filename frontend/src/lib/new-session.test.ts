/**
  * 功能：通用「新建会话」归属测试。
 *
 * 承诺行为：**任何上下文下（项目会话 / 员工单聊 / 已选项目）点通用「新建会话」，
 * 一律回到未选中项目的普通会话新建态**——否则用户在没有普通会话时永远建不出普通会话。
 * 项目内会话由项目面板「秘书 / 团队群聊」头像行的新建入口创建，员工单聊由员工行点击进入。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import { startNewSession } from "@/lib/new-session";
import { useChatStore } from "@/stores/chat-store";

function mockNavigate() {
  return vi.fn();
}

describe("startNewSession", () => {
  beforeEach(() => {
    useChatStore.setState({
      activeSessionId: null,
      activeProjectId: "",
      pendingSessionOrigin: "",
    });
    window.history.replaceState({}, "", "/");
  });

  it("项目上下文下 → 清空项目、回首页（不再是项目会话）", () => {
    window.history.replaceState({}, "", "/?project=p1");
    useChatStore.setState({ activeProjectId: "p1" });
    const navigate = mockNavigate();

    startNewSession(navigate);

    expect(useChatStore.getState().activeProjectId).toBe("");
    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("员工单聊视图下 → 同样回普通会话新建态", () => {
    window.history.replaceState({}, "", "/chat?view=dm&target=dev&project=p1");
    useChatStore.setState({ activeProjectId: "p1" });
    const navigate = mockNavigate();

    startNewSession(navigate);

    expect(useChatStore.getState().activeProjectId).toBe("");
    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("清空当前会话引用与待定来源（不残留上下文）", () => {
    window.history.replaceState({}, "", "/?project=p1");
    useChatStore.setState({ activeSessionId: "s1", pendingSessionOrigin: "secretary" });
    const navigate = mockNavigate();

    startNewSession(navigate);

    expect(useChatStore.getState().activeSessionId).toBeNull();
    expect(useChatStore.getState().pendingSessionOrigin).toBe("");
  });

  it("已在未选中态 → 行为不变（幂等）", () => {
    const navigate = mockNavigate();

    startNewSession(navigate);

    expect(useChatStore.getState().activeProjectId).toBe("");
    expect(navigate).toHaveBeenCalledWith("/");
  });
});
