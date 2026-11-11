/**
 * 功能：设置弹窗测试——-模型档案区（列表/激活/删除确认/编辑透传）
 * + R3左侧导航与用量/缓存页签。
 * 钉行为不钉实现：断言端点调用与界面状态，不看内部结构。
 *
 * ⚠️ 起弹窗默认页签 = **用量**（用户口径：进设置先看用量），故访问模型档案区
 * 需先点左侧导航「模型配置」——`openProfilesTab()` 收敛该动作。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsDialog } from "@/components/layout/SettingsDialog";
import { EMPTY_USAGE_STATS, settingsApi } from "@/services/settings";
import { useUiStore } from "@/stores/ui-store";

vi.mock("@/services/settings", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/settings")>();
  return {
    ...actual,
    settingsApi: {
      ...actual.settingsApi,
      profiles: vi.fn(),
      model: vi.fn(),
      activateProfile: vi.fn(),
      deleteProfile: vi.fn(),
      usageStats: vi.fn(),
    },
  };
});

vi.mock("@/components/chat/ModelConfigDialog", () => ({
  // -配置弹窗本体已有独立契约，这里只需确认入口可用（避免重复渲染重依赖）
  // 并把收到的 profile 以 data 属性暴露——编辑透传（掩码 key/extra）可被断言。
  ModelConfigDialog: ({
    open,
    profile,
  }: {
    open: boolean;
    profile?: { api_key_masked?: string; extra?: Record<string, string | number> };
  }) =>
    open ? (
      <div
        data-testid="model-config-dialog"
        data-key={profile?.api_key_masked ?? ""}
        data-extra={JSON.stringify(profile?.extra ?? null)}
      />
    ) : null,
}));

const profileA = {
  profile_id: "p-a",
  name: "主档案",
  provider: "deepseek",
  model: "deepseek-flash",
  api_key: "sk-abc***xyz",
  base_url: "",
  extra: { thinking_mode: "low" },
  has_api_key: true,
};
const profileB = {
  profile_id: "p-b",
  name: "备用",
  provider: "openai",
  model: "gpt-5.2",
  api_key: "",
  base_url: "",
  extra: {},
  has_api_key: false,
};

function openProfilesTab() {
  fireEvent.click(screen.getByTestId("settings-nav-profiles"));
}

describe("SettingsDialog 模型档案（137c-A1）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(settingsApi.profiles).mockResolvedValue({
      active_profile_id: "p-a",
      profiles: [profileA, profileB],
    });
    vi.mocked(settingsApi.model).mockResolvedValue({
      current_model: "deepseek-flash",
      has_api_key: true,
      available_models: [],
    });
    vi.mocked(settingsApi.activateProfile).mockResolvedValue({
      active_profile_id: "p-b",
      profile: null,
    });
    vi.mocked(settingsApi.deleteProfile).mockResolvedValue({
      deleted: "p-b",
      active_profile_id: "p-a",
    });
    vi.mocked(settingsApi.usageStats).mockResolvedValue({ ...EMPTY_USAGE_STATS });
  });

  it("渲染档案列表：激活态标记 + 无密钥提示", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-row-p-a")).toBeInTheDocument());
    expect(screen.getByTestId("profile-row-p-a")).toHaveTextContent("使用中");
    expect(screen.getByTestId("profile-row-p-b")).toHaveTextContent("未配置密钥");
    // 激活键对已激活行禁用（不可重复激活）
    expect(screen.getByTestId("profile-activate-p-a")).toBeDisabled();
    expect(screen.getByTestId("profile-activate-p-b")).not.toBeDisabled();
  });

  it("激活：点「应用」调 activateProfile、刷新列表并通知模型控制条重取", async () => {
    const before = useUiStore.getState().modelsVersion;
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-activate-p-b")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("profile-activate-p-b"));
    await waitFor(() => expect(settingsApi.activateProfile).toHaveBeenCalledWith("p-b"));
    expect(vi.mocked(settingsApi.profiles).mock.calls.length).toBeGreaterThan(1);
    // 设置页切换档案后，会话框模型控制条须重取（员工未配独立模型时跟随新档案）
    await waitFor(() => expect(useUiStore.getState().modelsVersion).toBe(before + 1));
  });

  it("状态语义：使用中=主色勾+应用按钮禁用；未使用=无勾+应用按钮可用", async () => {
    const { container } = render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-row-p-a")).toBeInTheDocument());

    const activeRow = screen.getByTestId("profile-row-p-a");
    // 使用中：主色打勾（换回之前的样式）且应用按钮禁用
    const activeMark = screen.getByTestId("profile-active-mark-p-a");
    expect(activeMark.querySelector("svg")).not.toBeNull();
    expect(activeMark.querySelector("svg")?.getAttribute("class")).toContain("text-primary");
    expect(screen.getByTestId("profile-activate-p-a")).toBeDisabled();
    // 拍板：「使用中」按钮 = 卡片底色 + 同色边缘线（原 disabled 黑按钮灰饼感太重）
    const activeBtn = screen.getByTestId("profile-activate-p-a");
    expect(activeBtn.className).toContain("bg-primary/5");
    expect(activeBtn.className).toContain("border-primary/5");
    expect(activeBtn.className).toContain("disabled:opacity-100");
    // （第二轮更正）：使用中的**模型条目胶囊不再变灰**——去底色/底色描边
    expect(activeRow.className).toContain("border-chat-hairline");
    expect(activeRow.className).not.toContain("bg-primary/5");
    expect(screen.getByTestId("profile-row-p-b").className).toContain("border-chat-hairline");
    // 未使用：无勾（不渲染 svg）、应用按钮可用
    const idleMark = screen.getByTestId("profile-active-mark-p-b");
    expect(idleMark.querySelector("svg")).toBeNull();
    expect(screen.getByTestId("profile-activate-p-b")).not.toBeDisabled();
    // 应用按钮在删除按钮右侧（DOM 顺序）
    const buttons = Array.from(activeRow.querySelectorAll("button"));
    const deleteIndex = buttons.findIndex((el) => el.dataset.testid === "profile-delete-p-a");
    const applyIndex = buttons.findIndex((el) => el.dataset.testid === "profile-activate-p-a");
    expect(applyIndex).toBeGreaterThan(deleteIndex);
    expect(container).toBeTruthy();
  });

  it("应用按钮为黑底白字主按钮（default 变体，与全局主操作统一；2026-10-01 用户拍板）", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-activate-p-b")).toBeInTheDocument());

    const apply = screen.getByTestId("profile-activate-p-b");
    expect(apply.dataset.variant, "应用按钮必须是 default 变体（黑底白字）").toBe("default");
    expect(apply.className).toContain("bg-primary");
    expect(apply.className).toContain("text-primary-foreground");
    // 破坏者：不得再回到黑边白底（outline + 自铺背景）
    expect(apply.className).not.toContain("border-foreground/70");
    expect(apply.className).not.toContain("bg-background");
  });

  it("删除：需确认；确认后调 deleteProfile 并用返回的 active_profile_id 回显", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-delete-p-b")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("profile-delete-p-b"));
    // 破坏者：确认前不得发删除请求
    expect(settingsApi.deleteProfile).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByTestId("profile-delete-confirm-p-b"));
    await waitFor(() => expect(settingsApi.deleteProfile).toHaveBeenCalledWith("p-b"));
  });

  it("破坏者：取消删除 → 不发请求（误点不毁档案）", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-delete-p-b")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("profile-delete-p-b"));
    fireEvent.click(await screen.findByText("取消"));
    await waitFor(() => expect(settingsApi.deleteProfile).not.toHaveBeenCalled());
  });

  it("新建入口：点击打开配置弹窗（新增态）", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-create")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("profile-create"));
    expect(await screen.findByTestId("model-config-dialog")).toBeInTheDocument();
  });

  it("137c-A1 修复：编辑点击必须透传掩码 key 与 extra（PUT 全量替换不得清空密钥/配置）", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    openProfilesTab();
    await waitFor(() => expect(screen.getByTestId("profile-edit-p-a")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("profile-edit-p-a"));
    const dialog = await screen.findByTestId("model-config-dialog");
    expect(dialog).toHaveAttribute("data-key", "sk-abc***xyz");
    expect(dialog).toHaveAttribute("data-extra", JSON.stringify({ thinking_mode: "low" }));
  });
});

describe("SettingsDialog 左侧导航与用量/缓存页签（R3）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(settingsApi.profiles).mockResolvedValue({
      active_profile_id: "",
      profiles: [],
    });
    vi.mocked(settingsApi.model).mockResolvedValue({
      current_model: "deepseek-v4.1-flash",
      has_api_key: false,
      available_models: [],
    });
    vi.mocked(settingsApi.usageStats).mockResolvedValue({
      ...EMPTY_USAGE_STATS,
      total: {
        ...EMPTY_USAGE_STATS.total,
        requests: 4,
        input_tokens: 1234,
        output_tokens: 56,
        cache_read_tokens: 900,
        cache_hit_rate: 0.729,
      },
      by_model: { "deepseek-v4.1-flash": { ...EMPTY_USAGE_STATS.total, input_tokens: 1234 } },
    });
  });

  it("默认打开「系统设置」页签，导航八项 + 底部关于齐全", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    for (const id of [
      "system",
      "usageCache",
      "profiles",
      "permissions",
      "data",
      "mcp",
      "capabilities",
      "computer",
      "about",
    ]) {
      expect(screen.getByTestId(`settings-nav-${id}`)).toBeInTheDocument();
    }
    await waitFor(() => expect(screen.getByTestId("settings-panel-system")).toBeInTheDocument());
    // 破坏者：默认页签不是模型配置
    expect(screen.queryByTestId("settings-profiles")).not.toBeInTheDocument();
  });

  it("用量与缓存**同屏**：指标卡与缓存明细必须同时可见（合并页签的核心）", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId("settings-nav-usageCache"));
    await waitFor(() => expect(screen.getByTestId("usage-total-input")).toBeInTheDocument());
    // 破坏者：二者若被拆到不同页签，这里必红
    expect(screen.getByTestId("cache-read")).toBeInTheDocument();
    expect(screen.getByTestId("usage-hit-rate")).toBeInTheDocument();
    // 破坏者：一次打开只允许一次聚合请求
    expect(vi.mocked(settingsApi.usageStats).mock.calls.length).toBe(1);
  });

  it("切区间：重新请求并带上新区间（7d）", async () => {
    render(<SettingsDialog open onOpenChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId("settings-nav-usageCache"));
    await waitFor(() => expect(settingsApi.usageStats).toHaveBeenCalledWith("all"));
    fireEvent.click(screen.getByTestId("usage-range-7d"));
    await waitFor(() => expect(settingsApi.usageStats).toHaveBeenCalledWith("7d"));
  });

  it("破坏者：弹窗关闭状态不得请求用量（避免无谓接口调用）", () => {
    render(<SettingsDialog open={false} onOpenChange={vi.fn()} />);
    expect(settingsApi.usageStats).not.toHaveBeenCalled();
  });

  it("关闭后再打开：必须重取聚合数据（不得复用陈旧数字）", async () => {
    const { rerender } = render(<SettingsDialog open onOpenChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId("settings-nav-usageCache"));
    await waitFor(() => expect(settingsApi.usageStats).toHaveBeenCalledTimes(1));
    rerender(<SettingsDialog open={false} onOpenChange={vi.fn()} />);
    rerender(<SettingsDialog open onOpenChange={vi.fn()} />);
    await waitFor(() => expect(settingsApi.usageStats).toHaveBeenCalledTimes(2));
  });
});
