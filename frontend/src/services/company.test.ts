/**
 * 功能：会话/公司域纯函数测试（项目→会话两级分组 / 单聊线程匹配）。
 */
import { describe, expect, it } from "vitest";

import { findDirectThread, type ThreadSummary } from "@/services/company";
import { groupSessionsByProject, type SessionSummary } from "@/services/sessions";

function session(id: string, projectId: string): SessionSummary {
  return { id, title: id, created_at: "", message_count: 0, project_id: projectId };
}

describe("groupSessionsByProject", () => {
  it("按项目分桶并保留输入顺序；无项目归空串组", () => {
    const groups = groupSessionsByProject([
      session("a", "p-alpha"),
      session("b", ""),
      session("c", "p-alpha"),
      session("d", "p-beta"),
    ]);
    expect([...groups.keys()]).toEqual(["p-alpha", "", "p-beta"]);
    expect(groups.get("p-alpha")?.map((item) => item.id)).toEqual(["a", "c"]);
    expect(groups.get("")?.map((item) => item.id)).toEqual(["b"]);
  });

  it("空输入返回空 Map", () => {
    expect(groupSessionsByProject([]).size).toBe(0);
  });
});

describe("findDirectThread", () => {
  const threads: ThreadSummary[] = [
    {
      thread_id: "t1",
      parent_task_id: null,
      participants: ["user", "worker"],
      created_at: "2026-09-22T01:00:00",
      message_count: 3,
    },
    {
      thread_id: "t2",
      parent_task_id: null,
      participants: ["worker"],
      created_at: "2026-09-22T02:00:00",
      message_count: 1,
    },
  ];

  it("参与者集合精确匹配（user + target）才命中", () => {
    expect(findDirectThread(threads, "worker")?.thread_id).toBe("t1");
    expect(findDirectThread(threads, "ghost")).toBeNull();
  });

  it("多匹配时取创建时间最新的一条（最新优先口径，不依赖后端返回顺序）", () => {
    const multi: ThreadSummary[] = [
      {
        thread_id: "old",
        parent_task_id: null,
        participants: ["user", "worker"],
        created_at: "2026-09-21T01:00:00",
        message_count: 1,
      },
      {
        thread_id: "new",
        parent_task_id: null,
        participants: ["user", "worker"],
        created_at: "2026-09-23T01:00:00",
        message_count: 1,
      },
    ];
    expect(findDirectThread(multi, "worker")?.thread_id).toBe("new");
    expect(findDirectThread([...multi].reverse(), "worker")?.thread_id).toBe("new");
  });
});
