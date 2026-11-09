/**
 * 功能：设置面板测试——字号 6 档落 store、权限档位写入、检索 Key 提交、MCP 开关与试连。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  AboutPanel,
  CapabilitiesPanel,
  McpPanel,
  PermissionsPanel,
  SystemPanel,
} from "@/components/settings/SettingsPanels";
import { useUiStore } from "@/stores/ui-store";

const settingsApi = {
  preferences: vi.fn(),
  savePreferences: vi.fn(),
  permissionRules: vi.fn(),
  savePermissionRules: vi.fn(),
  approvalGrants: vi.fn(),
  revokeApprovalGrant: vi.fn(),
  mcpServers: vi.fn(),
  patchMcpServer: vi.fn(),
  testMcpServer: vi.fn(),
  saveMcpGlobal: vi.fn(),
  systemInfo: vi.fn(),
  storage: vi.fn(),
  retentionPreview: vi.fn(),
  retentionCleanup: vi.fn(),
  toolsMeta: vi.fn(),
  skillsList: vi.fn(),
};

vi.mock("@/services/settings", () => ({
  settingsApi: {
    preferences: () => settingsApi.preferences(),
    savePreferences: (body: unknown) => settingsApi.savePreferences(body),
    permissionRules: () => settingsApi.permissionRules(),
    savePermissionRules: (rules: unknown) => settingsApi.savePermissionRules(rules),
    approvalGrants: () => settingsApi.approvalGrants(),
    revokeApprovalGrant: (body: unknown) => settingsApi.revokeApprovalGrant(body),
    mcpServers: () => settingsApi.mcpServers(),
    patchMcpServer: (name: string, body: unknown) => settingsApi.patchMcpServer(name, body),
    testMcpServer: (name: string) => settingsApi.testMcpServer(name),
    saveMcpGlobal: (body: unknown) => settingsApi.saveMcpGlobal(body),
    systemInfo: () => settingsApi.systemInfo(),
    storage: () => settingsApi.storage(),
    retentionPreview: (days: number) => settingsApi.retentionPreview(days),
    retentionCleanup: (days: number) => settingsApi.retentionCleanup(days),
    toolsMeta: () => settingsApi.toolsMeta(),
    skillsList: () => settingsApi.skillsList(),
  },
}));

const PREFERENCES = {
  permission: {
    ask_risk_levels: ["high", "critical"],
    ask_tools: ["bash", "write"],
    bypass_tools: ["read"],
    deny_tools: [],
    deny_commands: ["rm -rf /", "format *", "del /f /s", "rd /s /q"],
  },
  retention_days: 90,
  web_search: { tavily_configured: false, brave_configured: false },
};

beforeEach(() => {
  Object.values(settingsApi).forEach((fn) => fn.mockReset());
  settingsApi.preferences.mockResolvedValue(PREFERENCES);
  settingsApi.permissionRules.mockResolvedValue({ rules: [] });
  settingsApi.approvalGrants.mockResolvedValue({ grants: [] });
  settingsApi.systemInfo.mockResolvedValue({
    version: "0.1.0",
    python: "3.12.10",
    uvicorn: "0.30.0",
    platform: "Windows 11",
    data_dir: "/data",
    uptime_s: 120,
  });
});

describe("SystemPanel", () => {
  it("拖动字号滑杆到第 6 档 → uiScale=1.3 且提示同步", async () => {
    useUiStore.setState({ uiScale: 1 });
    render(<SystemPanel />);

    const slider = screen.getByTestId("settings-font-slider") as HTMLInputElement;
    // range 控件直接改值 + 派发 change（userEvent 的 clear 只支持可编辑元素）
    fireEvent.change(slider, { target: { value: "5" } });

    expect(useUiStore.getState().uiScale).toBeCloseTo(1.3);
    expect(screen.getByTestId("settings-font-current")).toHaveTextContent("130%");
  });

  it("填 Tavily Key 保存 → 只提交非空字段", async () => {
    settingsApi.savePreferences.mockResolvedValue(PREFERENCES);
    render(<SystemPanel />);

    await userEvent.type(screen.getByTestId("settings-tavily-key"), "tvly-abc");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(settingsApi.savePreferences).toHaveBeenCalledWith({
        web_search: { tavily_api_key: "tvly-abc" },
      }),
    );
    expect((screen.getByTestId("settings-tavily-key") as HTMLInputElement).value).toBe("");
  });

  it("破坏者：两个 Key 都为空 → 不发请求", async () => {
    render(<SystemPanel />);
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(settingsApi.savePreferences).not.toHaveBeenCalled();
  });
});

describe("PermissionsPanel", () => {
  it("当前配置不属于任何档位 → 状态条显示「自定义」而不是假装标准", async () => {
    render(<PermissionsPanel />);
    const status = await screen.findByTestId("permission-status");
    expect(status).toHaveTextContent("自定义");
    expect(status).toHaveTextContent("与三档预设都不完全一致");
    expect(status).toHaveTextContent("会话授权");
  });

  it("选严格档 → 出现未保存条，点应用才提交 permission_preset=strict", async () => {
    settingsApi.savePreferences.mockResolvedValue(PREFERENCES);
    render(<PermissionsPanel />);

    await userEvent.click(await screen.findByTestId("permission-preset-strict"));
    expect(screen.getByTestId("permission-savebar")).toHaveTextContent("档位将改为「严格」");
    expect(settingsApi.savePreferences).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "应用档位" }));
    await waitFor(() =>
      expect(settingsApi.savePreferences).toHaveBeenCalledWith({
        permission_preset: "strict",
      }),
    );
    expect(screen.queryByTestId("permission-savebar")).toBeNull();
  });

  it("取消未保存改动 → 不发请求、保存条消失", async () => {
    render(<PermissionsPanel />);
    await userEvent.click(await screen.findByTestId("permission-preset-loose"));
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByTestId("permission-savebar")).toBeNull());
    expect(settingsApi.savePreferences).not.toHaveBeenCalled();
  });

  it("档位卡带差异行（多问 / 少问），不是只给一句说明", async () => {
    render(<PermissionsPanel />);
    const strict = await screen.findByTestId("permission-preset-strict");
    expect(strict).toHaveTextContent("+ 多问 medium 级");
    expect(strict).toHaveTextContent("不再免询问");
    const loose = screen.getByTestId("permission-preset-loose");
    expect(loose).toHaveTextContent("− 不再问 high 级");
  });

  it("deny 底线常显，展开后列出具体命令", async () => {
    render(<PermissionsPanel />);
    expect(await screen.findByTestId("permission-deny-line")).toHaveTextContent(
      "任何档位都不会放宽",
    );
    await userEvent.click(screen.getByTestId("permission-floor-btn"));
    const sheet = await screen.findByTestId("permission-floor-sheet");
    expect(sheet).toHaveTextContent("rm -rf /");
    expect(sheet).toHaveTextContent("rd /s /q");
  });

  it("放行规则页常显优先级链与 allow 会压过档位的提示", async () => {
    render(<PermissionsPanel />);
    await userEvent.click(await screen.findByTestId("permission-tab-rules"));
    const prio = await screen.findByTestId("permission-priority");
    expect(prio).toHaveTextContent("规则 deny");
    expect(prio).toHaveTextContent("deny 底线");
    expect(prio).toHaveTextContent("规则 allow");
    expect(prio).toHaveTextContent("会让档位的「逐个确认」失效");
  });

  it("添加 ask 规则 → 提交 pattern/action 并刷新列表", async () => {
    settingsApi.savePermissionRules.mockResolvedValue({
      rules: [
        {
          pattern: "bash:echo:*",
          action: "ask",
          kind: "prefix",
          tool: "bash",
          content: "echo",
          source: "user",
        },
      ],
    });
    render(<PermissionsPanel />);
    await userEvent.click(await screen.findByTestId("permission-tab-rules"));
    await screen.findByTestId("permission-rules-panel");
    await userEvent.type(screen.getByTestId("permission-rule-pattern"), "bash:echo:*");
    await userEvent.click(screen.getByRole("button", { name: "添加规则" }));

    await waitFor(() =>
      expect(settingsApi.savePermissionRules).toHaveBeenCalledWith([
        { pattern: "bash:echo:*", action: "ask" },
      ]),
    );
    expect(screen.getByTestId("permission-rules-list")).toHaveTextContent("bash:echo:*");
  });

  it("同 pattern 不同 action：删一条不连带删另一条", async () => {
    settingsApi.permissionRules.mockResolvedValue({
      rules: [
        { pattern: "read", action: "allow", kind: "", tool: "read", content: "", source: "user" },
        { pattern: "read", action: "deny", kind: "", tool: "read", content: "", source: "user" },
      ],
    });
    render(<PermissionsPanel />);
    await userEvent.click(await screen.findByTestId("permission-tab-rules"));
    await userEvent.click(await screen.findByTestId("permission-rule-delete-allow-read"));

    await waitFor(() =>
      expect(settingsApi.savePermissionRules).toHaveBeenCalledWith([
        { pattern: "read", action: "deny" },
      ]),
    );
  });

  it("临时放行默认收起，展开后标出「整个工具」并可撤销", async () => {
    settingsApi.approvalGrants.mockResolvedValue({
      grants: [
        {
          session_key: "sess-1",
          tool_name: "bash",
          params_digest: "d1",
          scope: "session",
          granted_at: 1,
          expires_at: 1893456000,
          remaining_s: 60,
        },
      ],
    });
    settingsApi.revokeApprovalGrant.mockResolvedValue({ revoked: true });
    render(<PermissionsPanel />);
    await userEvent.click(await screen.findByTestId("permission-tab-rules"));
    expect(screen.queryByTestId("approval-grants-panel")).toBeNull();

    await userEvent.click(screen.getByTestId("permission-grants-fold"));
    const panel = await screen.findByTestId("approval-grants-panel");
    expect(panel).toHaveTextContent("整个工具 · 任意参数");

    await userEvent.click(screen.getByTestId("approval-grant-revoke-bash"));
    await waitFor(() =>
      expect(settingsApi.revokeApprovalGrant).toHaveBeenCalledWith({
        session_key: "sess-1",
        tool_name: "bash",
        params_digest: "d1",
        scope: "session",
      }),
    );
  });
});

describe("McpPanel", () => {
  const payload = {
    servers: [
      {
        name: "playwright",
        pool: "",
        transport: "stdio",
        command: "node",
        args: [],
        url: "",
        prefix: "",
        enabled: true,
        category: "浏览器",
        homepage: "https://example.com",
        allowed_tools: null,
        status: "connected" as const,
        tool_count: 12,
        tools: ["mcp__playwright__browser_click"],
      },
    ],
    global: { autostart: true, lazy_exposure: true, max_exposed_tools: 5 },
  };

  it("关闭服务器 → PATCH enabled=false；试连调用对应端点", async () => {
    settingsApi.mcpServers.mockResolvedValue(payload);
    settingsApi.patchMcpServer.mockResolvedValue({ ok: true });
    settingsApi.testMcpServer.mockResolvedValue({ ok: true, tool_count: 12, error: "" });
    render(<McpPanel />);

    await userEvent.click(await screen.findByTestId("mcp-switch-playwright"));
    await waitFor(() =>
      expect(settingsApi.patchMcpServer).toHaveBeenCalledWith("playwright", { enabled: false }),
    );

    await userEvent.click(screen.getByTestId("mcp-test-playwright"));
    await waitFor(() => expect(settingsApi.testMcpServer).toHaveBeenCalledWith("playwright"));
  });

  it("破坏者：服务器已停用 → 试连按钮禁用", async () => {
    settingsApi.mcpServers.mockResolvedValue({
      ...payload,
      servers: [{ ...payload.servers[0], enabled: false, status: "disabled" as const }],
    });
    render(<McpPanel />);
    expect(await screen.findByTestId("mcp-test-playwright")).toBeDisabled();
  });
});

describe("AboutPanel", () => {
  it("渲染版本与 Python 版本（来自 /system/info）", async () => {
    render(<AboutPanel />);
    expect(await screen.findByText("0.1.0")).toBeInTheDocument();
    expect(screen.getByText("3.12.10")).toBeInTheDocument();
  });
});

describe("CapabilitiesPanel", () => {
  const many = Array.from({ length: 120 }, (_, index) => ({
    name: `tool_${index}`,
    category: index % 2 === 0 ? "util" : "web",
    risk_level: index % 3 === 0 ? "high" : "low",
    mcp: index === 0,
  }));

  beforeEach(() => {
    vi.clearAllMocks();
    // 后端 /tools/meta 返回数组（不是 {tools:[...]}）
    settingsApi.toolsMeta.mockResolvedValue(many);
    settingsApi.skillsList.mockResolvedValue({
      skills: [{ name: "better-icons", description: "图标", tags: ["icon"], always: true }],
    });
  });

  it("工具目录完整渲染（不截断到 40）+ 计数正确", async () => {
    render(<CapabilitiesPanel />);
    expect(await screen.findByTestId("caps-count")).toHaveTextContent("显示 120 / 共 120");
    // 破坏者：若写死 slice(0,40) 或把数组当对象取 .tools，这里必红
    expect(screen.getAllByTestId("caps-tool-row")).toHaveLength(120);
  });

  it("搜索与分组过滤只影响展示，不改总数", async () => {
    render(<CapabilitiesPanel />);
    await screen.findByTestId("caps-count");
    fireEvent.change(screen.getByTestId("caps-search"), { target: { value: "tool_1" } });
    expect(screen.getByTestId("caps-count")).toHaveTextContent("共 120");
    expect(screen.getAllByTestId("caps-tool-row").length).toBeLessThan(120);

    fireEvent.change(screen.getByTestId("caps-search"), { target: { value: "" } });
    fireEvent.change(screen.getByTestId("caps-group"), { target: { value: "web" } });
    expect(screen.getAllByTestId("caps-tool-row")).toHaveLength(60);
  });

  it("MCP 工具带标记；技能页显示标签", async () => {
    render(<CapabilitiesPanel />);
    await screen.findByTestId("caps-count");
    expect(screen.getAllByTestId("caps-tool-row")[0]).toHaveTextContent("MCP");

    fireEvent.click(screen.getByTestId("caps-tab-skills"));
    expect(await screen.findByText("better-icons")).toBeInTheDocument();
    expect(screen.getByText("icon")).toBeInTheDocument();
  });
});
