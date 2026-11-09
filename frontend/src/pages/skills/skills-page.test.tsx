import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SkillsPage from "@/pages/skills";
import type { ResourceItem } from "@/services/resources";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  detail: vi.fn(),
  save: vi.fn(),
  setEnabled: vi.fn(),
  uninstall: vi.fn(),
  openFolder: vi.fn(),
  upload: vi.fn(),
  mcpServers: vi.fn(),
  patchMcpServer: vi.fn(),
  mcpConfig: vi.fn(),
  saveMcpConfig: vi.fn(),
}));

vi.mock("@/services/resources", () => ({
  resourcesApi: {
    list: mocks.list,
    detail: mocks.detail,
    save: mocks.save,
    setEnabled: mocks.setEnabled,
    uninstall: mocks.uninstall,
    openFolder: mocks.openFolder,
    upload: mocks.upload,
  },
}));

vi.mock("@/pages/skills/SopEditor", () => ({
  SopEditor: ({ resourceId, onBack }: { resourceId: string; onBack: () => void }) => (
    <div>
      <h1>{`SOP 编辑器：${resourceId}`}</h1>
      <button type="button" onClick={onBack}>
        返回列表
      </button>
    </div>
  ),
}));

vi.mock("@/services/settings", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/settings")>();
  return {
    ...actual,
    settingsApi: {
      ...actual.settingsApi,
      mcpServers: mocks.mcpServers,
      patchMcpServer: mocks.patchMcpServer,
      mcpConfig: mocks.mcpConfig,
      saveMcpConfig: mocks.saveMcpConfig,
    },
  };
});

function resource(overrides: Partial<ResourceItem>): ResourceItem {
  return {
    id: "sku-research",
    kind: "skill",
    title: "选品调研",
    description: "从类目趋势与竞品价格中提炼候选 SKU。",
    enabled: true,
    scope: { global: true, employees: [], team_id: "" },
    path: "~/.bandi/skills/sku-research/SKILL.md",
    folder: "~/.bandi/skills/sku-research",
    updated_at: "2026-10-29T10:00:00+08:00",
    meta: {},
    ...overrides,
  };
}

const SKILL = resource({});
const SOP = resource({
  id: "new-product-launch",
  kind: "sop",
  title: "新品上架 SOP",
  description: "从资料收集到渠道发布的上架流程。",
  enabled: false,
  path: "~/.bandi/skills/new-product-launch.card.json",
  folder: "~/.bandi/skills",
});
const DETAIL_CONTENT = `---
name: 选品调研
description: 从类目趋势与竞品价格中提炼候选 SKU。
---

# 选品调研

## 执行步骤
1. 收集类目趋势`;

beforeEach(() => {
  vi.clearAllMocks();
  mocks.list.mockImplementation(async (kind: string) => ({
    items: kind === "skill" ? [SKILL] : kind === "sop" ? [SOP] : [],
  }));
  mocks.detail.mockResolvedValue({
    item: SKILL,
    content: DETAIL_CONTENT,
  });
  mocks.save.mockImplementation(async (_kind: string, _id: string, content: string) => ({
    ok: true,
    item: { ...SKILL, description: content },
  }));
  mocks.setEnabled.mockImplementation(async (_kind: string, _id: string, enabled: boolean) => ({
    ok: true,
    item: { ...SKILL, enabled },
  }));
  mocks.uninstall.mockResolvedValue({ ok: true, archived: "~/.bandi/_uninstalled/skills/x" });
  mocks.openFolder.mockResolvedValue({ ok: true, opened: "~/.bandi/skills/sku-research" });
  mocks.upload.mockResolvedValue({ ok: true, item: SKILL });
  mocks.mcpServers.mockResolvedValue({
    servers: [
      {
        name: "filesystem",
        pool: "filesystem",
        transport: "stdio",
        command: "npx",
        args: ["-y", "@modelcontextprotocol/server-filesystem", "D:/work"],
        url: "",
        prefix: "",
        enabled: true,
        category: "",
        homepage: "",
        allowed_tools: null,
        status: "connected",
        tool_count: 12,
        tools: [],
      },
    ],
    global: { autostart: true, lazy_exposure: true, max_exposed_tools: 5 },
  });
  mocks.patchMcpServer.mockResolvedValue({ ok: true });
  mocks.mcpConfig.mockResolvedValue({
    path: "~/.bandi/mcp.json",
    content: {
      mcpServers: {
        filesystem: { command: "npx", args: ["-y", "fs"] },
      },
    },
  });
  mocks.saveMcpConfig.mockResolvedValue({ ok: true, path: "~/.bandi/mcp.json", count: 1 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("SkillsPage 资源中心", () => {
  it("渲染技能列表并在当前分类内搜索", async () => {
    render(<SkillsPage />);
    const card = await screen.findByTestId("resource-card-skill-sku-research");
    expect(within(card).getByText("选品调研")).toBeInTheDocument();
    expect(screen.getByText("SOP")).toBeInTheDocument();
    expect(screen.getByText("连接器")).toBeInTheDocument();

    const search = screen.getByPlaceholderText("搜索当前分类");
    fireEvent.change(search, { target: { value: "不存在" } });
    await waitFor(() => {
      expect(screen.queryByTestId("resource-card-skill-sku-research")).not.toBeInTheDocument();
    });
    expect(screen.getByText("没有匹配的结果")).toBeInTheDocument();
  });

  it("卡片开关只切换状态，不打开详情", async () => {
    const user = userEvent.setup();
    render(<SkillsPage />);
    const card = await screen.findByTestId("resource-card-skill-sku-research");
    await user.click(within(card).getByRole("switch"));
    await waitFor(() => {
      expect(mocks.setEnabled).toHaveBeenCalledWith("skill", "sku-research", false);
    });
    expect(screen.queryByText("返回列表")).not.toBeInTheDocument();
  });

  it("点击卡片打开详情，源码态支持 Ctrl+S 保存", async () => {
    const user = userEvent.setup();
    render(<SkillsPage />);
    await user.click(await screen.findByTestId("resource-card-skill-sku-research"));

    expect(await screen.findByText("执行步骤")).toBeInTheDocument();
    expect(screen.getByText("返回列表")).toBeInTheDocument();
    await user.click(screen.getByLabelText("源码显示"));
    const source = screen.getByLabelText("资源源码") as HTMLTextAreaElement;
    expect(source.value).toContain("选品调研");

    fireEvent.change(source, { target: { value: `${source.value}\n\n## 备注` } });
    await user.keyboard("{Control>}s{/Control}");
    await waitFor(() => {
      expect(mocks.save).toHaveBeenCalled();
    });
    expect(screen.getByText("已保存")).toBeInTheDocument();
  });

  it("未保存返回弹窗：取消不保存，确认保存并返回", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<SkillsPage />);
    await user.click(await screen.findByTestId("resource-card-skill-sku-research"));
    await user.click(await screen.findByLabelText("源码显示"));
    const source = screen.getByLabelText("资源源码") as HTMLTextAreaElement;
    fireEvent.change(source, { target: { value: `${source.value}\n\n## 临时修改` } });
    await user.click(screen.getByText("返回列表"));

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("保存更改？")).toBeInTheDocument();
    await user.click(within(dialog).getByText("取消"));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(mocks.save).not.toHaveBeenCalled();
    unmount();

    render(<SkillsPage />);
    await user.click(await screen.findByTestId("resource-card-skill-sku-research"));
    await user.click(await screen.findByLabelText("源码显示"));
    const source2 = screen.getByLabelText("资源源码") as HTMLTextAreaElement;
    fireEvent.change(source2, { target: { value: `${source2.value}\n\n## 确认保存` } });
    await user.click(screen.getByText("返回列表"));
    const dialog2 = await screen.findByRole("alertdialog");
    await user.click(within(dialog2).getByText("确认"));
    await waitFor(() => {
      expect(mocks.save).toHaveBeenCalled();
    });
  });

  it("SOP 卡片提供可视化编辑入口并进入编辑器", async () => {
    const user = userEvent.setup();
    render(<SkillsPage />);
    await screen.findByTestId("resource-card-skill-sku-research");
    await user.click(screen.getByText("SOP"));
    const card = await screen.findByTestId("resource-card-sop-new-product-launch");
    await user.click(within(card).getByRole("button", { name: "可视化编辑" }));
    expect(await screen.findByText("SOP 编辑器：new-product-launch")).toBeInTheDocument();
  });

  it("连接器 Tab 打开 MCP 配置弹窗并保存 JSON", async () => {
    const user = userEvent.setup();
    render(<SkillsPage />);
    await screen.findByTestId("resource-card-skill-sku-research");
    await user.click(screen.getByText("连接器"));

    const connector = await screen.findByTestId("resource-card-mcp-filesystem");
    expect(within(connector).getByText("12 个工具")).toBeInTheDocument();
    expect(screen.getByText("自定义连接器")).toBeInTheDocument();

    await user.click(screen.getByText("自定义连接器"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("MCP 服务管理")).toBeInTheDocument();
    await user.click(within(dialog).getByText("配置"));
    expect(await within(dialog).findByText("~/.bandi/mcp.json")).toBeInTheDocument();

    const source = within(dialog).getByLabelText("MCP 配置 JSON") as HTMLTextAreaElement;
    fireEvent.change(source, { target: { value: "{}" } });
    await user.click(within(dialog).getByText("保存"));
    await waitFor(() => {
      expect(within(dialog).getByText("配置需要包含 mcpServers 对象")).toBeInTheDocument();
    });

    fireEvent.change(source, {
      target: { value: '{"mcpServers":{"demo":{"command":"npx","args":["-y","demo"]}}}' },
    });
    await user.click(within(dialog).getByText("保存"));
    await waitFor(() => {
      expect(mocks.saveMcpConfig).toHaveBeenCalledWith({
        mcpServers: { demo: { command: "npx", args: ["-y", "demo"] } },
      });
    });
  });
});
