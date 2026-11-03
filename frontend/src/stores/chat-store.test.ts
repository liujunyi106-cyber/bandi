/**
  * 功能：chat-store 行为测试。
 * 钉行为不钉实现：断言渲染产物（消息内容与状态），不依赖 React 渲染。
 */
import { beforeEach, describe, expect, it } from "vitest";

import {
  applyDelta,
  applyMemoryWritten,
  applyNote,
  applyProgressNode,
  applySkillInjected,
  applyThinking,
  applyToolProgress,
  applyToolResult,
  applyToolRetryResult,
  applyToolStart,
  entriesToMessages,
  finalizeMessage,
  markToolSkipped,
  normalizeTaskLedger,
  sessionTaskLedger,
  useChatStore,
  type ChatMessage,
  type ProcessNode,
} from "@/stores/chat-store";

function resetStore(): void {
  useChatStore.setState({
    sessions: [],
    sessionsLoaded: false,
    activeSessionId: null,
    activeProjectId: "",
    messagesBySession: {},
    pendingSendsBySession: {},
    streams: {},
    interactions: {},
    notifications: [],
    stagedRevert: null,
    tasksBySession: {},
    employeeProgress: {},
    employeeProgressDone: {},
  });
}

function baseMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    sessionId: "s1",
    role: "assistant",
    content: "",
    done: false,
    ...overrides,
  };
}

describe("pure reducers", () => {
  it("entriesToMessages 过滤 tool/空内容并保留有内容条目", () => {
    const messages = entriesToMessages("s1", [
      { id: "e1", entry_type: "message", turn_index: 1, role: "user", content: "你好" },
      { id: "e2", entry_type: "message", turn_index: 1, role: "tool", content: "工具结果" },
      { id: "e3", entry_type: "message", turn_index: 1, role: "assistant", content: "  " },
      { id: "e4", entry_type: "message", turn_index: 2, role: "assistant", content: "回答" },
    ]);
    expect(messages.map((message) => message.id)).toEqual(["e1", "e4"]);
    expect(messages[1]).toMatchObject({ role: "assistant", content: "回答", done: true });
    // entryId：后端条目才带（fork/revert 等 entry 级操作的可用性依据）
    expect(messages.map((message) => message.entryId)).toEqual(["e1", "e4"]);
  });

  it("员工回写条目携带发送者身份（senderRole/senderName 快照；刷新后仍显示员工）", () => {
    const messages = entriesToMessages("s1", [
      {
        id: "e1",
        entry_type: "message",
        turn_index: 3,
        role: "assistant",
        content: "主图已出：main-v2.png",
        metadata: JSON.stringify({
          employee_reply: true,
          sender_role: "team_designer",
          sender_name: "美工设计",
        }),
      },
    ]);
    expect(messages).toHaveLength(1);
    expect(messages[0]).toMatchObject({
      role: "assistant",
      senderRole: "team_designer",
      senderName: "美工设计",
    });
  });

  it("员工回写条目带过程快照（process_events → 过程块回放；刷新后可回看）", () => {
    const messages = entriesToMessages("s1", [
      {
        id: "e1",
        entry_type: "message",
        turn_index: 3,
        role: "assistant",
        content: "产出完成",
        metadata: JSON.stringify({
          employee_reply: true,
          sender_role: "team_designer",
          sender_name: "美工设计",
          process_events: [
            { type: "tool_call_start", data: { tool_name: "read", tool_call_id: "t1" } },
            {
              type: "tool_result",
              data: {
                tool_name: "read",
                success: true,
                output_preview: "ok",
                duration_ms: 5,
                tool_call_id: "t1",
              },
            },
          ],
        }),
      },
    ]);
    expect(messages).toHaveLength(1);
    const process = messages[0].process ?? [];
    expect(process.length).toBeGreaterThan(0);
    expect(process.some((node) => node.kind === "tool" && node.toolName === "read")).toBe(true);
  });

  it("历史重建收尾：process_events 尾部无 done 帧时 running 统一结算（防刷新后永久「执行中」）", () => {
    const messages = entriesToMessages("s1", [
      {
        id: "e1",
        entry_type: "message",
        turn_index: 3,
        role: "assistant",
        content: "产出完成",
        metadata: JSON.stringify({
          employee_reply: true,
          sender_role: "team_designer",
          sender_name: "美工设计",
          // 回写时机截断：只有中间态帧、无 done——重建必须收尾
          process_events: [
            { type: "tool_call_start", data: { tool_name: "read", tool_call_id: "t1" } },
            { type: "thinking_delta", data: { text: "再想想" } },
          ],
        }),
      },
    ]);
    const process = messages[0].process ?? [];
    expect(process.length).toBeGreaterThan(0);
    expect(process.every((node) => node.status !== "running")).toBe(true);
  });

  it("破坏者：process_events 坏形态（非数组/含非对象项）→ 不崩、跳过坏项", () => {
    const messages = entriesToMessages("s1", [
      {
        id: "e1",
        entry_type: "message",
        turn_index: 1,
        role: "assistant",
        content: "坏形态",
        metadata: JSON.stringify({
          employee_reply: true,
          sender_role: "r",
          process_events: "not-an-array",
        }),
      },
      {
        id: "e2",
        entry_type: "message",
        turn_index: 2,
        role: "assistant",
        content: "混合",
        metadata: JSON.stringify({
          employee_reply: true,
          sender_role: "r",
          process_events: [null, 42, { type: "thinking_delta", data: { text: "想" } }],
        }),
      },
    ]);
    expect(messages[0].process).toBeUndefined();
    expect(messages[1].process?.length).toBeGreaterThan(0);
  });

  it("破坏者：普通助手条目不得被误标员工身份（无标记则不带 senderRole/senderName）", () => {
    const messages = entriesToMessages("s1", [
      { id: "e1", entry_type: "message", turn_index: 1, role: "assistant", content: "秘书回复" },
      {
        id: "e2",
        entry_type: "message",
        turn_index: 2,
        role: "assistant",
        content: "缺标记的条目",
        metadata: JSON.stringify({ sender_role: "team_designer" }),
      },
    ]);
    expect(messages[0].senderRole).toBeUndefined();
    expect(messages[0].senderName).toBeUndefined();
    expect(messages[1].senderRole).toBeUndefined();
  });

  it("applyDelta 只命中目标 pending 槽（keyed 归并）", () => {
    const list = [baseMessage({ id: "p1" }), baseMessage({ id: "p2", content: "other" })];
    const next = applyDelta(list, "p1", "增量");
    expect(next[0].content).toBe("增量");
    expect(next[1].content).toBe("other");
  });

  it("applyToolResult 匹配最后一个同名 running 节点（LIFO）", () => {
    const list = [
      baseMessage({
        id: "p1",
        process: [
          { id: "t1", kind: "tool", toolName: "bash", status: "running" },
          { id: "t2", kind: "tool", toolName: "bash", status: "running" },
        ],
      }),
    ];
    const next = applyToolResult(list, "p1", {
      toolName: "bash",
      success: false,
      preview: "boom",
      durationMs: 12,
    });
    expect(next[0].process?.[0].status).toBe("running");
    expect(next[0].process?.[1]).toMatchObject({ status: "error", detail: "boom", durationMs: 12 });
  });

  it("applyThinking：同段思考增量累积，工具后新思考开新节点", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "先看");
    list = applyThinking(list, "p1", "目录");
    list = applyToolStart(list, "p1", "ls", { path: "." });
    list = applyThinking(list, "p1", "再读文件");
    const nodes = list[0].process ?? [];
    expect(nodes).toHaveLength(3);
    expect(nodes[0]).toMatchObject({ kind: "thought", text: "先看目录", status: "success" });
    expect(nodes[1]).toMatchObject({ kind: "tool", toolName: "ls", input: { path: "." } });
    expect(nodes[2]).toMatchObject({ kind: "thought", text: "再读文件", status: "running" });
  });

  it("applyToolProgress：更新最后一个同名 running 工具节点（非命中不动）", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyToolStart(list, "p1", "web_fetch", { url: "https://x" });
    list = applyToolStart(list, "p1", "read");
    list = applyToolProgress(list, "p1", {
      toolName: "web_fetch",
      progressType: "status",
      data: { step: "fetch_started" },
    });
    const nodes = list[0].process ?? [];
    expect(nodes[0]).toMatchObject({
      kind: "tool",
      progress: { type: "status", data: { step: "fetch_started" } },
    });
    const second = nodes[1];
    if (second.kind !== "tool") throw new Error("第二个节点应为工具节点");
    expect(second.progress).toBeUndefined();
  });

  it("applyToolResult 完成后不再被后续进度覆盖（running 收窄）", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyToolStart(list, "p1", "ls");
    list = applyToolResult(list, "p1", {
      toolName: "ls",
      success: true,
      preview: "ok",
      durationMs: 3,
    });
    list = applyToolProgress(list, "p1", { toolName: "ls", progressType: "status", data: {} });
    const node = list[0].process?.[0];
    if (!node || node.kind !== "tool") throw new Error("首个节点应为工具节点");
    expect(node.progress).toBeUndefined();
  });

  it("finalizeMessage 收敛仍 running 的过程节点为 success", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "想了一半");
    list = applyToolStart(list, "p1", "ls");
    const next = finalizeMessage(list, "p1", { turnIndex: 2 });
    expect(next[0].process?.every((node) => node.status === "success")).toBe(true);
    expect(next[0].done).toBe(true);
  });

  it("applySkillInjected：技能名合并去重 + 次数累加 + 插过程块首位", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyThinking(list, "p1", "先想");
    list = applySkillInjected(list, "p1", ["git-workflow"]);
    list = applySkillInjected(list, "p1", ["git-workflow", "code-review"]);
    const nodes = list[0].process ?? [];
    expect(nodes[0]).toMatchObject({
      kind: "skill",
      skills: ["git-workflow", "code-review"],
      count: 2,
      status: "success",
    });
    expect(nodes[1]).toMatchObject({ kind: "thought", text: "先想" });
  });

  it("applyNote：相邻同类注记去重，不同类各记一条", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyNote(list, "p1", "compaction");
    list = applyNote(list, "p1", "compaction");
    list = applyNote(list, "p1", "interrupt");
    const nodes = list[0].process ?? [];
    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toMatchObject({ kind: "note", noteType: "compaction" });
    expect(nodes[1]).toMatchObject({ kind: "note", noteType: "interrupt" });
  });
});

describe("chat store keyed stream merge", () => {
  beforeEach(resetStore);

  it("端到端：本地发送 → 增量累积 → done 定稿", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "帮我看看");
    expect(useChatStore.getState().streams["s1"]?.sessionId).toBe("s1");

    store.appendDelta("s1", "它");
    store.appendDelta("s1", "好");
    store.finalizeStream("s1", { turnIndex: 3 });

    const list = useChatStore.getState().messagesBySession["s1"];
    expect(list.map((message) => message.role)).toEqual(["user", "assistant"]);
    expect(list[1].content).toBe("它好");
    expect(list[1].done).toBe(true);
    expect(list[1].turnIndex).toBe(3);
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
  });

  it("多会话不串：槽在 s1 时 s2 的增量不落 s1", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "第一句");
    store.appendDelta("s1", "A");
    store.appendDelta("s2", "B");
    const list = useChatStore.getState().messagesBySession["s1"];
    expect(list[1].content).toBe("A");
    expect(useChatStore.getState().messagesBySession["s2"]).toBeUndefined();
  });

  it("两会话可同时流式——各自槽独立，增量互不串台", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "A 的问题");
    store.appendLocalUser("s2", "B 的问题");
    expect(useChatStore.getState().streams["s1"]).toBeDefined();
    expect(useChatStore.getState().streams["s2"]).toBeDefined();

    store.appendDelta("s1", "A 的回复");
    store.appendDelta("s2", "B 的回复");

    const s1 = useChatStore.getState().messagesBySession["s1"];
    const s2 = useChatStore.getState().messagesBySession["s2"];
    expect(s1[1].content).toBe("A 的回复");
    expect(s2[1].content).toBe("B 的回复");

    useChatStore.getState().finalizeStream("s1", {});
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
    expect(useChatStore.getState().streams["s2"]).toBeDefined();
  });

  it("abortLocalStream 只释放指定会话，不影响另一会话", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "A");
    store.appendLocalUser("s2", "B");
    useChatStore.getState().abortLocalStream("s1");
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
    expect(useChatStore.getState().streams["s2"]).toBeDefined();
  });

  it("新会话预生成 id：追加/定稿全走同一 id（busy 期 abort/steer 可用）", () => {
    const store = useChatStore.getState();
    store.setActiveSession("sess-pre");
    store.appendLocalUser("sess-pre", "新会话第一句");
    store.appendDelta("sess-pre", "回复");
    store.finalizeStream("sess-pre", { turnIndex: 1 });

    const state = useChatStore.getState();
    expect(state.messagesBySession[""]).toBeUndefined();
    expect(state.messagesBySession["sess-pre"]).toHaveLength(2);
    expect(state.messagesBySession["sess-pre"][1].done).toBe(true);
  });

  it("failStream 标记错误并释放槽", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "x");
    store.failStream("s1", "内核错误");
    const list = useChatStore.getState().messagesBySession["s1"];
    expect(list[1].error).toBe("内核错误");
    expect(list[1].done).toBe(true);
    expect(useChatStore.getState().streams["s1"]).toBeUndefined();
  });

  it("工具事件挂过程节点（start → result 回填）", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "跑个命令");
    store.applyToolStart("s1", "bash");
    store.applyToolResult("s1", { toolName: "bash", success: true, preview: "ok", durationMs: 5 });
    const node = useChatStore.getState().messagesBySession["s1"][1].process?.[0];
    expect(node).toMatchObject({ toolName: "bash", status: "success", detail: "ok" });
  });

  it("appendEmployeeReply：员工工作回复入流（名字+角色标识保留）", () => {
    useChatStore.getState().appendEmployeeReply("s1", {
      name: "市场调研",
      roleId: "selection-team_market_researcher",
      content: "Mock回复：已核对价格带",
    });
    const list = useChatStore.getState().messagesBySession["s1"];
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({
      role: "assistant",
      content: "Mock回复：已核对价格带",
      senderName: "市场调研",
      senderRole: "selection-team_market_researcher",
      done: true,
    });
  });
});

describe("过程消息数据源（记忆行 / 任务台账）", () => {
  beforeEach(resetStore);

  it("applyMemoryWritten 追加记忆写入行（相邻同类型去重防重复帧）", () => {
    const list = [baseMessage({ id: "p1", process: [] })];
    const once = applyMemoryWritten(list, "p1", ["long_term", "episodic"]);
    const twice = applyMemoryWritten(once, "p1", ["long_term", "episodic"]);
    expect(once[0].process).toHaveLength(1);
    expect(once[0].process?.[0]).toMatchObject({
      kind: "note",
      noteType: "memory_written",
      count: 2,
    });
    expect(twice[0].process).toHaveLength(1);
  });

  it("破坏者：空 channels 不产生「记入记忆 x0」行", () => {
    const list = [baseMessage({ id: "p1", process: [] })];
    const next = applyMemoryWritten(list, "p1", []);
    expect(next[0].process).toHaveLength(0);
  });

  it("normalizeTaskLedger：未知状态归 pending、content 回退 title、脏条目丢弃", () => {
    const items = normalizeTaskLedger([
      { id: "1", title: "写契约", status: "in_progress" },
      { content: "跑回归", status: "done" },
      { title: "未知", status: "weird" },
      "not-a-dict",
    ]);
    expect(items).toHaveLength(3);
    expect(items[0]).toMatchObject({ title: "写契约", status: "in_progress" });
    expect(items[1]).toMatchObject({ content: "跑回归", status: "completed" });
    expect(items[2].status).toBe("pending");
  });

  it("sessionTaskLedger：metadata 字符串 / 对象 / 损坏三态", () => {
    expect(
      sessionTaskLedger({
        metadata: JSON.stringify({ task_ledger: [{ title: "a", status: "completed" }] }),
      }),
    ).toHaveLength(1);
    expect(sessionTaskLedger({ metadata: { task_ledger: [{ title: "b" }] } })[0].title).toBe("b");
    expect(sessionTaskLedger({ metadata: "{bad json" })).toEqual([]);
    expect(sessionTaskLedger(undefined)).toEqual([]);
  });

  it("setTaskLedger 写入会话任务台账（task_ledger 帧接线产物）", () => {
    useChatStore
      .getState()
      .setTaskLedger("s1", [{ id: "1", title: "t", content: "t", status: "pending" }]);
    expect(useChatStore.getState().tasksBySession["s1"]).toHaveLength(1);
    useChatStore.getState().setTaskLedger("s1", []);
    expect(useChatStore.getState().tasksBySession["s1"]).toHaveLength(0);
  });
});

describe("插队批（员工执行过程 thread_progress）", () => {
  beforeEach(resetStore);

  it("applyProgressNode：thinking 累积 + 工具 start→progress→result 归并", () => {
    let nodes: ProcessNode[] = [];
    nodes = applyProgressNode(nodes, { type: "thinking_delta", data: { text: "先想" } });
    nodes = applyProgressNode(nodes, { type: "thinking_delta", data: { text: "再想" } });
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "thought", text: "先想再想", status: "running" });

    nodes = applyProgressNode(nodes, {
      type: "tool_call_start",
      data: { tool_name: "web_search", tool_input: { query: "x" } },
    });
    expect(nodes[0]).toMatchObject({ kind: "thought", status: "success" });
    expect(nodes[1]).toMatchObject({ kind: "tool", toolName: "web_search", status: "running" });

    nodes = applyProgressNode(nodes, {
      type: "tool_progress",
      data: { tool_name: "web_search", progress_type: "status", data: { step: "fetch" } },
    });
    expect((nodes[1] as { progress?: { type: string } }).progress?.type).toBe("status");

    nodes = applyProgressNode(nodes, {
      type: "tool_result",
      data: { tool_name: "web_search", success: true, output_preview: "ok", duration_ms: 12 },
    });
    expect(nodes[1]).toMatchObject({ status: "success", detail: "ok", durationMs: 12 });
  });

  it("破坏者：done/error 终态必须收尾所有 running 节点（快照回放不得残留「正在执行」）", () => {
    // 回复已落地/失败 + 页面刷新后，过程块仍显示「正在执行」
    // 根因是 done/error 落入 default 不收尾，回放的 running 节点永久悬浮。
    let nodes: ProcessNode[] = [];
    nodes = applyProgressNode(nodes, { type: "thinking_delta", data: { text: "想" } });
    nodes = applyProgressNode(nodes, {
      type: "tool_call_start",
      data: { tool_name: "web_search", tool_input: {} },
    });
    nodes = applyProgressNode(nodes, { type: "thinking_delta", data: { text: "再想" } });
    expect(nodes.some((node) => node.status === "running")).toBe(true);

    const settled = applyProgressNode(nodes, { type: "done", data: { terminal_reason: "COMPLETED" } });
    expect(settled.some((node) => node.status === "running")).toBe(false);
    expect(settled[0]).toMatchObject({ kind: "thought", status: "success" });
    expect(settled[1]).toMatchObject({ kind: "tool", status: "success" });

    const failed = applyProgressNode(nodes, { type: "error", data: { message: "boom" } });
    expect(failed.some((node) => node.status === "running")).toBe(false);
  });

  it("skill/memory/note 归并去重；content 与未知事件不产生节点（破坏者）", () => {
    let nodes: ProcessNode[] = [];
    nodes = applyProgressNode(nodes, { type: "skill_injected", data: { skills: ["a"] } });
    nodes = applyProgressNode(nodes, { type: "skill_injected", data: { skills: ["b"] } });
    expect(nodes[0]).toMatchObject({ kind: "skill", count: 2 });

    nodes = applyProgressNode(nodes, {
      type: "memory_written",
      data: { channels: ["long_term"] },
    });
    nodes = applyProgressNode(nodes, {
      type: "memory_written",
      data: { channels: ["long_term"] },
    });
    expect(nodes.filter((node) => node.kind === "note")).toHaveLength(1);

    const before = nodes.length;
    nodes = applyProgressNode(nodes, { type: "content_delta", data: { text: "x" } });
    nodes = applyProgressNode(nodes, { type: "unknown_event", data: {} });
    expect(nodes).toHaveLength(before);
  });

  it("applyEmployeeProgress：finish 收敛 running 并保留；新执行重置；clear 双清", () => {
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "旧思路" },
    });
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"][0].status).toBe("running");

    // done 帧 → finish：running 收敛 success（完成态） + 标记（槽保留可回顾）
    useChatStore.getState().finishEmployeeProgress("p1", "t1", "r1");
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"][0].status).toBe("success");

    // 新执行首帧 → 清槽重建（无旧节点残留、无陈旧标记）
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "新思路" },
    });
    const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "thought", text: "新思路" });
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();

    useChatStore.getState().clearEmployeeProgress("p1", "t1", "r1");
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toBeUndefined();
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
  });

  it("consumeEmployeeProgress：取出完成态节点并清空双槽（回复固化路径）", () => {
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r1", { type: "thinking_delta", data: { text: "x" } });
    useChatStore.getState().finishEmployeeProgress("p1", "t1", "r1");

    const nodes = useChatStore.getState().consumeEmployeeProgress("p1", "t1", "r1");
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "thought", status: "success" });
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toBeUndefined();
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
    // 空槽幂等（返回空数组）
    expect(useChatStore.getState().consumeEmployeeProgress("p1", "t1", "r1")).toEqual([]);
  });

  it("expireEmployeeProgressIfDone：完成态兜底清槽；在跑的新执行不被误清（破坏者）", () => {
    // 完成态 + 无人消费 → 兜底清槽（防过程块永久悬浮在视图底部）
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r1", { type: "thinking_delta", data: { text: "收尾" } });
    useChatStore.getState().finishEmployeeProgress("p1", "t1", "r1");
    useChatStore.getState().expireEmployeeProgressIfDone("p1", "t1", "r1");
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toBeUndefined();
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();

    // 新执行已开始（无完成标记）→ 兜底动作必须 no-op，不得清掉在跑的过程
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r1", { type: "thinking_delta", data: { text: "在跑" } });
    useChatStore.getState().expireEmployeeProgressIfDone("p1", "t1", "r1");
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);
  });

  it("破坏者（REV-137b2-6）：空 project 写、company 读 → 必须命中同一槽", () => {
    useChatStore
      .getState()
      .applyEmployeeProgress("", "t1", "r1", { type: "thinking_delta", data: { text: "空项目" } });
    useChatStore.getState().finishEmployeeProgress("company", "t1", "r1");
    // 写侧空值 + 读侧 "company" 若不同口径，这里会取到空数组（原实现的分叉点）
    const nodes = useChatStore.getState().consumeEmployeeProgress("company", "t1", "r1");
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "thought", text: "空项目" });
  });

  it("破坏者（REV-137b2-9）：断线结算把仍在执行中的槽收敛为完成态 + 断线注记", () => {
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "干到一半" },
    });
    // 断线前：无完成标记（槽仍在「执行中」）——这正是断线错过终态帧后的状态
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();

    const settled = useChatStore.getState().settleStaleEmployeeProgress();
    expect(settled).toEqual(["p1::t1::r1"]);

    const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toMatchObject({ kind: "thought", status: "success" });
    // 注记让「过程可能不完整」这件事对用户可见，而不是静默补个句号
    expect(nodes[1]).toMatchObject({ kind: "note", noteType: "disconnected" });
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);

    // 幂等：再次结算不重复加注记（避免每次短暂断网都叠一行）
    expect(useChatStore.getState().settleStaleEmployeeProgress()).toEqual([]);
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(2);

    // 回复仍能消费到（含注记）并清槽
    const consumed = useChatStore.getState().consumeEmployeeProgress("p1", "t1", "r1");
    expect(consumed).toHaveLength(2);
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toBeUndefined();
  });

  it("对照组（REV-137b2-9）：已完成槽与空槽不被断线结算改动", () => {
    // 已完成（等回复接手）→ 结算不动它，仍走兜底过期/回复消费
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "已完成" },
    });
    useChatStore.getState().finishEmployeeProgress("p1", "t1", "r1");
    // 无内容槽（该员工没在跑：未知/不产节点的事件只开出一个空数组）→ 不结算、不挂标记
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r2", { type: "content_delta", data: {} });

    expect(useChatStore.getState().settleStaleEmployeeProgress()).toEqual([]);
    expect(useChatStore.getState().employeeProgress["p1::t1::r1"]).toHaveLength(1);
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);
    // 空槽保持空（结算只碰「有内容且未完成」的槽——没有可结算的内容就不该加注记）
    expect(useChatStore.getState().employeeProgress["p1::t1::r2"]).toHaveLength(0);
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r2"]).toBeUndefined();
  });

  it("破坏者（REV-137b2-9）：结算后该执行其实还在跑 → 首帧清空重建（自愈、不叠注记）", () => {
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r1", { type: "thinking_delta", data: { text: "旧" } });
    useChatStore.getState().settleStaleEmployeeProgress();

    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r1", { type: "thinking_delta", data: { text: "新" } });
    const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "thought", text: "新", status: "running" });
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
  });

  it("破坏者（REV-137b2-11）：同一员工两条线程各跑一次 → 过程块互不串槽", () => {
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "tA", "r1", { type: "thinking_delta", data: { text: "任务A" } });
    useChatStore.getState().finishEmployeeProgress("p1", "tA", "r1");
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "tB", "r1", { type: "thinking_delta", data: { text: "任务B" } });
    useChatStore.getState().finishEmployeeProgress("p1", "tB", "r1");

    // 回复到达顺序与执行顺序相反（模拟补发/乱序）：各自只取自己那一次的槽
    const forB = useChatStore.getState().consumeEmployeeProgress("p1", "tB", "r1");
    expect(forB).toHaveLength(1);
    expect(forB[0]).toMatchObject({ text: "任务B" });
    const forA = useChatStore.getState().consumeEmployeeProgress("p1", "tA", "r1");
    expect(forA).toHaveLength(1);
    expect(forA[0]).toMatchObject({ text: "任务A" });
    // 两槽都已消费 → 清空
    expect(useChatStore.getState().employeeProgress["p1::tA::r1"]).toBeUndefined();
    expect(useChatStore.getState().employeeProgress["p1::tB::r1"]).toBeUndefined();
  });

  it("破坏者（REV-137b2-11）：补发的终态帧不得清空已定稿的过程块（幂等）", () => {
    // 断线结算留下「部分过程 + 注记」
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "跑到一半" },
    });
    useChatStore.getState().settleStaleEmployeeProgress();
    const before = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(before).toHaveLength(2);

    // 重连后服务端补发终态帧（终态帧入回放缓冲，会再来一次）
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", { type: "done", data: {} });
    const after = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(after).toHaveLength(2);
    expect(after).toEqual(before);
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBe(true);
  });

  it("对照组（REV-137b2-11）：非终态新帧仍会清空重建（新执行开始语义不变）", () => {
    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "上一轮" },
    });
    useChatStore.getState().finishEmployeeProgress("p1", "t1", "r1");

    useChatStore.getState().applyEmployeeProgress("p1", "t1", "r1", {
      type: "thinking_delta",
      data: { text: "下一轮" },
    });
    const nodes = useChatStore.getState().employeeProgress["p1::t1::r1"];
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ text: "下一轮", status: "running" });
    expect(useChatStore.getState().employeeProgressDone["p1::t1::r1"]).toBeUndefined();
  });

  it("expireEmployeeProgressByKey：按槽键兜底清槽（断线结算路径）", () => {
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t1", "r1", { type: "thinking_delta", data: { text: "x" } });
    useChatStore.getState().settleStaleEmployeeProgress();
    const [key] = Object.keys(useChatStore.getState().employeeProgress);
    useChatStore.getState().expireEmployeeProgressByKey(key);
    expect(useChatStore.getState().employeeProgress[key]).toBeUndefined();
    expect(useChatStore.getState().employeeProgressDone[key]).toBeUndefined();
    // 无完成标记的槽不被误清（与 expireEmployeeProgressIfDone 同守则）
    useChatStore
      .getState()
      .applyEmployeeProgress("p1", "t2", "r1", { type: "thinking_delta", data: { text: "在跑" } });
    useChatStore.getState().expireEmployeeProgressByKey("p1::t2::r1");
    expect(useChatStore.getState().employeeProgress["p1::t2::r1"]).toHaveLength(1);
  });
});

describe("notifications", () => {
  beforeEach(resetStore);

  it("推送按新→旧排序 + 标记已读 + 清空", () => {
    const store = useChatStore.getState();
    store.pushNotification({ kind: "done", title: "第一条" });
    store.pushNotification({ kind: "error", title: "第二条" });
    const list = useChatStore.getState().notifications;
    expect(list[0].title).toBe("第二条");
    expect(list.filter((item) => !item.read)).toHaveLength(2);

    useChatStore.getState().markNotificationsRead();
    expect(useChatStore.getState().notifications.every((item) => item.read)).toBe(true);
    useChatStore.getState().clearNotifications();
    expect(useChatStore.getState().notifications).toHaveLength(0);
  });
});

describe("会话回收（removeSessionLocal）", () => {
  beforeEach(resetStore);

  it("删除会话同时回收任务台账与挂起交互（防 map 键常驻增长）", () => {
    const store = useChatStore.getState();
    store.appendLocalUser("s1", "你好");
    store.setTaskLedger("s1", [{ id: "1", title: "写契约", content: "写契约", status: "pending" }]);
    store.addInteraction({ interaction_id: "i1", session_id: "s1", kind: "confirm", payload: {} });
    store.appendLocalUser("s2", "别的会话");

    useChatStore.getState().removeSessionLocal("s1");

    const after = useChatStore.getState();
    expect(after.messagesBySession["s1"]).toBeUndefined();
    expect(after.tasksBySession["s1"]).toBeUndefined();
    expect(after.interactions["i1"]).toBeUndefined();
    expect(after.messagesBySession["s2"]).toBeDefined();
  });
});

describe("abortLocalStream", () => {
  beforeEach(resetStore);

  it("有内容的挂起消息 → 定稿 done + 槽释放（保留已产出部分）", () => {
    useChatStore.setState({
      streams: { s1: { sessionId: "s1", pendingId: "m1" } },
      messagesBySession: { s1: [baseMessage({ content: "部分回复" })] },
    });
    useChatStore.getState().abortLocalStream("s1");
    const state = useChatStore.getState();
    expect(state.streams["s1"]).toBeUndefined();
    expect(state.messagesBySession["s1"]?.[0]?.done).toBe(true);
    expect(state.messagesBySession["s1"]?.[0]?.content).toBe("部分回复");
  });

  it("空占位（无内容无过程）→ 直接移除（不残留空气泡）", () => {
    useChatStore.setState({
      streams: { s1: { sessionId: "s1", pendingId: "m1" } },
      messagesBySession: { s1: [baseMessage({ content: "  " })] },
    });
    useChatStore.getState().abortLocalStream("s1");
    const state = useChatStore.getState();
    expect(state.streams["s1"]).toBeUndefined();
    expect(state.messagesBySession["s1"] ?? []).toHaveLength(0);
  });

  it("无槽 → no-op（不抛错、不动消息）", () => {
    useChatStore.setState({ streams: {}, messagesBySession: {} });
    expect(() => useChatStore.getState().abortLocalStream("s1")).not.toThrow();
    expect(useChatStore.getState().messagesBySession).toEqual({});
  });
});

describe("137b-3b 失败工具重试/跳过（节点级）", () => {
  it("applyToolStart/applyToolResult 携带 toolCallId（帧字段透传到节点）", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyToolStart(list, "p1", "bash", { cmd: "ls" }, "call_9");
    expect((list[0].process?.[0] as { toolCallId?: string }).toolCallId).toBe("call_9");
    list = applyToolResult(list, "p1", {
      toolName: "bash",
      success: false,
      preview: "boom",
      durationMs: 8,
      toolCallId: "call_9",
    });
    expect(list[0].process?.[0]).toMatchObject({ status: "error", toolCallId: "call_9" });
  });

  it("旧帧无 tool_call_id 时节点不带 toolCallId（不可重试形态）", () => {
    let list = [baseMessage({ id: "p1" })];
    list = applyToolStart(list, "p1", "bash", { cmd: "ls" });
    expect((list[0].process?.[0] as { toolCallId?: string }).toolCallId).toBeUndefined();
  });

  it("markToolSkipped：仅 error 工具节点变 skipped；其他节点不动", () => {
    let list = [
      baseMessage({
        id: "p1",
        process: [
          { id: "n1", kind: "tool", toolName: "bash", status: "error" },
          { id: "n2", kind: "tool", toolName: "read", status: "success" },
          { id: "n3", kind: "thought", text: "想", status: "success" },
        ] as ProcessNode[],
      }),
    ];
    list = markToolSkipped(list, "n1");
    expect(list[0].process?.[0]).toMatchObject({ status: "skipped" });
    expect(list[0].process?.[1]).toMatchObject({ status: "success" });
    // 非 error 定位（伪造 nodeId 指向 success 节点）→ 原样
    const next = markToolSkipped(list, "n2");
    expect(next[0].process?.[1]).toMatchObject({ status: "success" });
  });

  it("applyToolRetryResult：按 toolCallId 回填重试结果（只更新内存节点）", () => {
    let list = [
      baseMessage({
        id: "p1",
        process: [
          { id: "n1", kind: "tool", toolName: "bash", status: "error", toolCallId: "call_9" },
        ] as ProcessNode[],
      }),
    ];
    list = applyToolRetryResult(list, "call_9", {
      success: true,
      outputPreview: "fixed output",
      durationMs: 42,
    });
    expect(list[0].process?.[0]).toMatchObject({
      status: "success",
      detail: "fixed output",
      durationMs: 42,
    });
    // 未知 toolCallId → 不动
    const untouched = applyToolRetryResult(list, "ghost", {
      success: false,
      outputPreview: "",
      durationMs: 0,
    });
    expect(untouched[0].process?.[0]).toMatchObject({ status: "success" });
  });

  it("store 动作：skipToolNode / applyToolRetry 按会话生效", () => {
    resetStore();
    useChatStore.setState({
      activeSessionId: "s1",
      messagesBySession: {
        s1: [
          baseMessage({
            id: "m1",
            process: [
              { id: "n1", kind: "tool", toolName: "bash", status: "error", toolCallId: "call_9" },
            ] as ProcessNode[],
          }),
        ],
      },
    });
    useChatStore.getState().skipToolNode("s1", "n1");
    expect(useChatStore.getState().messagesBySession.s1?.[0].process?.[0]).toMatchObject({
      status: "skipped",
    });
    useChatStore.getState().applyToolRetry("s1", "call_9", {
      success: true,
      outputPreview: "ok",
      durationMs: 5,
    });
    expect(useChatStore.getState().messagesBySession.s1?.[0].process?.[0]).toMatchObject({
      status: "success",
      detail: "ok",
    });
  });
});

describe("137b-3c 过程条目还原（detail 解析恢复）", () => {
  it("entriesToMessages：process 条目节点挂到前一条 assistant 消息（含 skipped 态）", () => {
    const messages = entriesToMessages("s1", [
      { id: "e1", entry_type: "message", turn_index: 1, role: "user", content: "跑一下" },
      { id: "e2", entry_type: "message", turn_index: 1, role: "assistant", content: "执行完成" },
      {
        id: "e3",
        entry_type: "process",
        turn_index: 1,
        role: "assistant",
        content: "",
        metadata: JSON.stringify({
          process: [
            {
              id: "pr:tool:1",
              kind: "tool",
              toolName: "bash",
              input: {},
              status: "skipped",
              toolCallId: "call_1",
            },
            { id: "pr:thought:1", kind: "thought", text: "想一想", status: "success" },
          ],
        }),
      },
    ]);
    expect(messages).toHaveLength(2);
    const assistant = messages[1];
    expect(assistant.process).toHaveLength(2);
    expect(assistant.process?.[0]).toMatchObject({ kind: "tool", status: "skipped" });
    expect(assistant.process?.[1]).toMatchObject({ kind: "thought", text: "想一想" });
  });

  it("entriesToMessages：process 条目缺失/空/坏 JSON → 不挂过程、不崩", () => {
    const base = [
      { id: "e1", entry_type: "message", turn_index: 1, role: "assistant", content: "回复" },
    ];
    const bare = entriesToMessages("s1", [
      ...base,
      { id: "e2", entry_type: "process", turn_index: 1, role: "assistant", content: "" },
    ]);
    expect(bare[0].process).toBeUndefined();
    const broken = entriesToMessages("s1", [
      ...base,
      {
        id: "e3",
        entry_type: "process",
        turn_index: 1,
        role: "assistant",
        content: "",
        metadata: "{oops",
      },
    ]);
    expect(broken[0].process).toBeUndefined();
  });
});

describe("draftReferences", () => {
  it("增删清 + 同路径去重（不叠加 chips）", () => {
    useChatStore.setState({ draftReferences: [] });
    useChatStore.getState().addDraftReference({ path: "a.md", name: "a.md" });
    useChatStore.getState().addDraftReference({ path: "a.md", name: "a.md" });
    useChatStore.getState().addDraftReference({ path: "b/x.md", name: "x.md" });
    expect(useChatStore.getState().draftReferences).toHaveLength(2);
    useChatStore.getState().removeDraftReference("a.md");
    expect(useChatStore.getState().draftReferences.map((ref) => ref.path)).toEqual(["b/x.md"]);
    useChatStore.getState().clearDraftReferences();
    expect(useChatStore.getState().draftReferences).toEqual([]);
  });
});

describe("entriesToMessages · checkpoint 条目不进对话", () => {
  it("role=null / content=空的 checkpoint 条目被过滤（不渲染空壳）", () => {
    const entries = [
      {
        id: "e1",
        session_id: "s1",
        parent_id: null,
        entry_type: "message",
        turn_index: 0,
        role: "user",
        content: "你好",
        tool_call_id: null,
        metadata: "{}",
        created_at: "2026-10-12T00:00:00",
      },
      {
        id: "c1",
        session_id: "s1",
        parent_id: null,
        entry_type: "checkpoint",
        turn_index: 1,
        role: null,
        content: "",
        tool_call_id: null,
        metadata: '{"turn_index":1}',
        created_at: "2026-10-12T00:00:01",
      },
    ] as never;
    const messages = entriesToMessages("s1", entries);
    expect(messages).toHaveLength(1);
    expect(messages[0].content).toBe("你好");
    expect(messages.some((m) => m.id === "c1")).toBe(false);
  });
});

describe("pendingSendsBySession · 执行中待发送队列", () => {
  beforeEach(resetStore);

  it("按会话入队/删除，文本与附件保留", () => {
    const store = useChatStore.getState();
    const id = store.enqueuePendingSend("s1", "排队", [
      { upload_id: "u1", name: "a.md", size: 3 },
    ]);
    expect(useChatStore.getState().pendingSendsBySession.s1).toHaveLength(1);
    expect(useChatStore.getState().pendingSendsBySession.s1[0]).toMatchObject({
      id,
      text: "排队",
      attachments: [{ upload_id: "u1", name: "a.md", size: 3 }],
    });

    store.removePendingSend("s1", id);
    expect(useChatStore.getState().pendingSendsBySession.s1).toBeUndefined();
  });

  it("会话删除时同步清空待发送队列", () => {
    const store = useChatStore.getState();
    store.upsertSession({
      id: "s1",
      title: "会话",
      created_at: "",
      message_count: 0,
      project_id: "p1",
    });
    store.enqueuePendingSend("s1", "排队");
    store.removeSessionLocal("s1");
    expect(useChatStore.getState().pendingSendsBySession.s1).toBeUndefined();
  });
});
