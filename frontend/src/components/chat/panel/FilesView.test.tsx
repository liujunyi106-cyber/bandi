/**
 * 功能：右栏文件视图测试（懒加载展开 / 文件点击开页签 / 定位高亮）。
 * .2：员工作用域两形态；员工态接项目文件树（projectFiles 端点 + 页签带 projectId）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FilesView } from "@/components/chat/panel/FilesView";
import type { PanelScope } from "@/lib/panel-scope";
import { panelApi } from "@/services/panel";
import { usePanelStore } from "@/stores/panel-store";

const SESSION_SCOPE: PanelScope = { kind: "session", key: "session:s1", sessionId: "s1" };
const EMPLOYEE_SCOPE: PanelScope = {
  kind: "employee",
  key: "employee:p1::dev",
  projectId: "p1",
  roleId: "dev",
};

vi.mock("@/services/panel", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/panel")>();
  return {
    ...actual,
    panelApi: {
      ...actual.panelApi,
      files: vi.fn(async (_sessionId: string, path = "") => ({
        session_id: "s1",
        root: "C:/ws",
        path,
        entries:
          path === ""
            ? [
                { name: "docs", type: "dir" as const, size: 0, mtime: 1 },
                { name: "notes.md", type: "file" as const, size: 10, mtime: 2 },
              ]
            : [{ name: "out.md", type: "file" as const, size: 20, mtime: 3 }],
      })),
      projectFiles: vi.fn(async (_projectId: string, path = "") => ({
        project_id: "p1",
        root: "C:/project",
        path,
        entries:
          path === ""
            ? [
                { name: "docs", type: "dir" as const, size: 0, mtime: 1 },
                { name: "notes.md", type: "file" as const, size: 10, mtime: 2 },
              ]
            : [{ name: "out.md", type: "file" as const, size: 20, mtime: 3 }],
      })),
    },
  };
});

describe("FilesView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    usePanelStore.setState({
      open: true,
      view: "files",
      tabs: [],
      activeTabId: "",
      fileFocusPath: "",
    });
  });

  it("懒加载：根级显示；点目录才拉取子级", async () => {
    render(<FilesView scope={SESSION_SCOPE} />);
    expect(await screen.findByTestId("panel-file-notes.md")).toBeInTheDocument();
    expect(panelApi.files).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("panel-dir-docs"));
    expect(await screen.findByTestId("panel-file-docs/out.md")).toBeInTheDocument();
    expect(panelApi.files).toHaveBeenCalledTimes(2);
  });

  it("点文件 → 打开文件页签（focusFile）", async () => {
    render(<FilesView scope={SESSION_SCOPE} />);
    fireEvent.click(await screen.findByTestId("panel-file-notes.md"));
    expect(usePanelStore.getState().activeTabId).toBe("file:notes.md");
    expect(usePanelStore.getState().fileFocusPath).toBe("notes.md");
  });

  it("G2 定位：fileFocusPath 自动逐级展开到目标文件", async () => {
    usePanelStore.setState({ fileFocusPath: "docs/out.md" });
    render(<FilesView scope={SESSION_SCOPE} />);
    expect(await screen.findByTestId("panel-file-docs/out.md")).toBeInTheDocument();
    await waitFor(() => expect(panelApi.files).toHaveBeenCalledWith("s1", "docs"));
  });

  it("员工作用域：项目文件树（数据源 projectFiles，不发会话文件请求）", async () => {
    render(<FilesView scope={EMPLOYEE_SCOPE} />);
    expect(await screen.findByTestId("panel-file-notes.md")).toBeInTheDocument();
    expect(panelApi.projectFiles).toHaveBeenCalledWith("p1", "");
    expect(panelApi.files).not.toHaveBeenCalled();
  });

  it("员工作用域：懒加载 + 点文件 → 页签带 projectId（预览走项目端点）", async () => {
    render(<FilesView scope={EMPLOYEE_SCOPE} />);
    fireEvent.click(await screen.findByTestId("panel-dir-docs"));
    expect(await screen.findByTestId("panel-file-docs/out.md")).toBeInTheDocument();
    expect(panelApi.projectFiles).toHaveBeenCalledWith("p1", "docs");

    fireEvent.click(screen.getByTestId("panel-file-notes.md"));
    expect(usePanelStore.getState().activeTabId).toBe("file:p1::notes.md");
    expect(usePanelStore.getState().tabs[0]?.projectId).toBe("p1");
    expect(usePanelStore.getState().fileFocusPath).toBe("notes.md");
  });
});
