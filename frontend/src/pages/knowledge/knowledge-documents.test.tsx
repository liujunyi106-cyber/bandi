import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentsTab } from "@/pages/knowledge/DocumentsTab";

const NODES = [
  {
    id: "f1",
    base_id: "b1",
    parent_id: "",
    kind: "folder",
    name: "制度",
    sort_order: 1,
    employee_id: null,
    size: 0,
    url: "",
    updated_at: 1,
  },
  {
    id: "d1",
    base_id: "b1",
    parent_id: "f1",
    kind: "md",
    name: "考勤制度.md",
    sort_order: 1,
    employee_id: null,
    size: 10,
    url: "",
    updated_at: 1,
  },
  {
    id: "d2",
    base_id: "b1",
    parent_id: "f1",
    kind: "csv",
    name: "名单.csv",
    sort_order: 2,
    employee_id: null,
    size: 20,
    url: "",
    updated_at: 1,
  },
];

interface FetchCall {
  url: string;
  method: string;
  body?: string;
}

function stubKnowledgeFetch(nodes: unknown[] = NODES): FetchCall[] {
  const calls: FetchCall[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = String(init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: typeof init?.body === "string" ? init.body : undefined });
    const json = (payload: unknown) =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    if (url.includes("/company/knowledge/bases")) {
      return json({
        bases: [{ base_id: "b1", name: "共享库", employee_id: "", document_count: 3 }],
      });
    }
    if (url.includes("/knowledge/nodes") && method === "GET") {
      return json({ nodes });
    }
    if (url.includes("/content") && method === "GET") {
      return json({ kind: "md", text: "", base_mtime: 1 });
    }
    if (url.includes("/view-state")) {
      return json({ state: {} });
    }
    return json({});
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

async function renderTab(query = "") {
  render(<DocumentsTab query={query} />);
  await screen.findByText("制度");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DocumentsTab 树与操作", () => {
  it("按扁平树渲染顶层节点，点击文件夹后展开子项", async () => {
    stubKnowledgeFetch();
    await renderTab();
    const tree = document.querySelector('[data-testid="knowledge-tree"]') as HTMLElement;
    expect(within(tree).queryByText("考勤制度.md")).not.toBeInTheDocument();
    await userEvent.click(within(tree).getByText("制度"));
    expect(await within(tree).findByText("考勤制度.md")).toBeInTheDocument();
    expect(within(tree).getByText("名单.csv")).toBeInTheDocument();
  });

  it("搜索命中文件时自动展开祖先并高亮命中片段", async () => {
    stubKnowledgeFetch();
    await renderTab("考勤");
    await waitFor(() => expect(document.querySelector('[data-node-id="d1"]')).not.toBeNull());
    expect(screen.getByText("制度")).toBeInTheDocument();
    const mark = document.querySelector("mark");
    expect(mark?.textContent).toBe("考勤");
  });

  it("删除确认按子树计算项数，并在确认后调用删除接口", async () => {
    const calls = stubKnowledgeFetch();
    await renderTab();
    const row = document.querySelector('[data-node-id="f1"]') as HTMLElement;
    fireEvent.mouseEnter(row);
    await userEvent.click(row.querySelector('button[aria-label="更多"]') as HTMLElement);
    await userEvent.click(await screen.findByText("删除"));
    expect(await screen.findByText(/将删除 3 项/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1) as HTMLElement);
    await waitFor(() =>
      expect(
        calls.some((call) => call.method === "DELETE" && call.url.includes("/knowledge/nodes/f1")),
      ).toBe(true),
    );
  });

  it("新建 Markdown 自动补 .md 后缀并提交到当前库", async () => {
    const calls = stubKnowledgeFetch();
    await renderTab();
    await userEvent.click(screen.getByRole("button", { name: "新建或上传" }));
    await userEvent.click(await screen.findByText("新建 Markdown"));
    const input = await screen.findByPlaceholderText("输入名称后回车");
    await userEvent.type(input, "周报");
    await userEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      const post = calls.find(
        (call) => call.method === "POST" && call.url.endsWith("/knowledge/nodes"),
      );
      expect(post).toBeTruthy();
      expect(post?.body).toContain('"name":"周报.md"');
    });
  });

  it("移动选择器排除自身与后代文件夹", async () => {
    stubKnowledgeFetch([
      ...NODES,
      {
        ...NODES[0],
        id: "f2",
        parent_id: "f1",
        name: "子文件夹",
      },
    ]);
    await renderTab();
    await userEvent.click(screen.getByText("制度"));
    await waitFor(() => expect(document.querySelector('[data-node-id="f2"]')).not.toBeNull());
    const row = document.querySelector('[data-node-id="f2"]') as HTMLElement;
    fireEvent.mouseEnter(row);
    await userEvent.click(row.querySelector('button[aria-label="更多"]') as HTMLElement);
    await userEvent.click(await screen.findByText("移动到…"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "根目录" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "制度" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "子文件夹" })).not.toBeInTheDocument();
  });
});
