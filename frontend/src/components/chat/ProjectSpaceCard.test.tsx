/**
 * 功能：项目空间卡片行为测试。
 * 断言口径：触发位文案随项目归属切换；菜单结构（搜索/项目/分隔线/两个创建入口）；
 * 两条创建路径的后端入参不同（create_folder=true vs root_dir=本地文件夹）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectSpaceCard } from "@/components/chat/ProjectSpaceCard";
import { companyApi } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";

vi.mock("@/services/company", () => ({
  companyApi: {
    projects: vi.fn(),
    createProject: vi.fn(),
    directories: vi.fn(),
    pickDirectory: vi.fn(),
  },
}));

const mocked = vi.mocked(companyApi);

function project(projectId: string, name: string) {
  return {
    project_id: projectId,
    name,
    root_dir: "",
    team_ids: [],
    created_at: "",
    updated_at: "",
    members: [],
  };
}

describe("ProjectSpaceCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ activeProjectId: "" });
    mocked.projects.mockResolvedValue({
      projects: [project("coffee-shop", "咖啡店小程序设计"), project("alpha", "Alpha 空间")],
    });
  });

  it("无项目归属：触发位显示「选择项目空间」，展开后是搜索 → 项目 → 分隔线 → 两个创建入口", async () => {
    render(<ProjectSpaceCard />);
    const trigger = screen.getByTestId("project-space-trigger");
    expect(trigger).toHaveTextContent("选择项目空间");

    fireEvent.click(trigger);

    expect(await screen.findByTestId("project-space-search")).toBeInTheDocument();
    expect(await screen.findByTestId("project-space-item-coffee-shop")).toHaveTextContent(
      "咖啡店小程序设计",
    );
    expect(screen.getByTestId("project-space-new")).toHaveTextContent("新建项目空间");
    expect(screen.getByTestId("project-space-open-local")).toHaveTextContent("打开本地文件夹");
  });

  it("有项目归属：触发位直接显示项目名称", async () => {
    useChatStore.setState({ activeProjectId: "coffee-shop" });
    render(<ProjectSpaceCard />);
    await waitFor(() =>
      expect(screen.getByTestId("project-space-trigger")).toHaveTextContent("咖啡店小程序设计"),
    );
  });

  it("触发位配色：常态与占位同色（浅灰）；hover / 菜单展开态变黑（2026-09-29 第三/五轮）", async () => {
    useChatStore.setState({ activeProjectId: "coffee-shop" });
    render(<ProjectSpaceCard />);
    await waitFor(() =>
      expect(screen.getByTestId("project-space-trigger")).toHaveTextContent("咖啡店小程序设计"),
    );
    const cls = screen.getByTestId("project-space-trigger").className;
    expect(cls).toContain("text-chat-muted/55");
    expect(cls).toContain("hover:text-chat-ink");
    expect(cls).toContain("data-[state=open]:text-chat-ink");
  });

  it("搜索框按名称过滤项目列表", async () => {
    render(<ProjectSpaceCard />);
    fireEvent.click(screen.getByTestId("project-space-trigger"));

    const search = await screen.findByTestId("project-space-search");
    fireEvent.change(search, { target: { value: "alpha" } });

    expect(screen.getByTestId("project-space-item-alpha")).toBeInTheDocument();
    expect(screen.queryByTestId("project-space-item-coffee-shop")).toBeNull();
  });

  it("选中项目：写入 activeProjectId", async () => {
    render(<ProjectSpaceCard />);
    fireEvent.click(screen.getByTestId("project-space-trigger"));
    fireEvent.click(await screen.findByTestId("project-space-item-alpha"));

    expect(useChatStore.getState().activeProjectId).toBe("alpha");
  });

  it("新建项目空间：弹窗文案正确，确认后 create_folder=true 并以新项目为项目空间", async () => {
    mocked.createProject.mockResolvedValue({ project: project("新空间", "新空间") });
    render(<ProjectSpaceCard />);
    fireEvent.click(screen.getByTestId("project-space-trigger"));
    fireEvent.click(await screen.findByTestId("project-space-new"));

    const dialog = await screen.findByTestId("new-project-space-dialog");
    expect(dialog).toHaveTextContent("新建项目空间");
    expect(dialog).toHaveTextContent(
      "为工作空间命名，本地将自动创建同名文件夹，命名后不可随意更改",
    );

    fireEvent.change(screen.getByTestId("new-project-space-name"), {
      target: { value: "新空间" },
    });
    fireEvent.click(screen.getByTestId("new-project-space-confirm"));

    await waitFor(() =>
      expect(mocked.createProject).toHaveBeenCalledWith({
        project_id: "新空间",
        name: "新空间",
        create_folder: true,
      }),
    );
    await waitFor(() => expect(useChatStore.getState().activeProjectId).toBe("新空间"));
  });

  it("打开本地文件夹：原生选择框返回路径 → 以该文件夹为 root_dir 建项目（名取路径末段）", async () => {
    mocked.pickDirectory.mockResolvedValue({
      path: "C:/Users/me/Desktop/咖啡店素材",
      fallback: false,
    });
    mocked.createProject.mockResolvedValue({
      project: project("咖啡店素材", "咖啡店素材"),
    });

    render(<ProjectSpaceCard />);
    fireEvent.click(screen.getByTestId("project-space-trigger"));
    fireEvent.click(await screen.findByTestId("project-space-open-local"));

    await waitFor(() =>
      expect(mocked.createProject).toHaveBeenCalledWith({
        project_id: "咖啡店素材",
        name: "咖啡店素材",
        root_dir: "C:/Users/me/Desktop/咖啡店素材",
      }),
    );
    // 原生可用时不得再弹内置选择器
    expect(screen.queryByTestId("local-folder-dialog")).toBeNull();
  });

  it("打开本地文件夹：原生不可用 → 回落内置选择器，列目录后确认同样以 root_dir 建项目", async () => {
    mocked.pickDirectory.mockResolvedValue({ path: "", fallback: true });
    mocked.directories.mockResolvedValue({
      path: "C:/Users/me/Desktop/咖啡店素材",
      parent: "C:/Users/me/Desktop",
      entries: [{ name: "子目录", path: "C:/Users/me/Desktop/咖啡店素材/子目录" }],
      is_root: false,
    });
    mocked.createProject.mockResolvedValue({
      project: project("咖啡店素材", "咖啡店素材"),
    });

    render(<ProjectSpaceCard />);
    fireEvent.click(screen.getByTestId("project-space-trigger"));
    fireEvent.click(await screen.findByTestId("project-space-open-local"));

    expect(await screen.findByTestId("local-folder-entry")).toHaveTextContent("子目录");
    fireEvent.click(screen.getByTestId("local-folder-confirm"));

    await waitFor(() =>
      expect(mocked.createProject).toHaveBeenCalledWith({
        project_id: "咖啡店素材",
        name: "咖啡店素材",
        root_dir: "C:/Users/me/Desktop/咖啡店素材",
      }),
    );
  });
});
