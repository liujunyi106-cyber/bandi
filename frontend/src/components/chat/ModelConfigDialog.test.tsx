/**
 * 功能：模型配置弹窗契约（Day137b2b-3）——创建自动激活、编辑回显掩码 key
 * 高级折叠区默认收起、校验失败不出请求、删除/激活回调。
 * settingsApi 用真实模块 + mock `@/services/api`（走服务层形状，不 mock 业务）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModelConfigDialog } from "@/components/chat/ModelConfigDialog";
import type { ModelOption, ProfileSummary } from "@/services/settings";
import { useUiStore } from "@/stores/ui-store";

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  del: vi.fn(),
}));

vi.mock("@/services/api", () => ({
  api: apiMocks,
  ApiError: class ApiError extends Error {},
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

const OPTIONS: ModelOption[] = [
  {
    name: "deepseek-v4-pro",
    provider: "deepseek",
    supports_reasoning: true,
    reasoning_efforts: ["low", "medium", "high", "max"],
  },
  { name: "gpt-4o", provider: "openai", supports_reasoning: false, reasoning_efforts: [] },
];

const PROFILE: ProfileSummary = {
  profile_id: "p1",
  name: "深寻主力",
  provider: "deepseek",
  model: "deepseek-v4-pro",
  base_url: "",
  api_key_masked: "sk-***5678",
};

describe("ModelConfigDialog（Day137b-2b）", () => {
  beforeEach(() => {
    vi.clearAllMocks(); // 清调用历史——calls[0] 断言不得吃到上一用例的请求
    apiMocks.get.mockResolvedValue({});
    apiMocks.post.mockResolvedValue({});
    apiMocks.put.mockResolvedValue({});
    apiMocks.del.mockResolvedValue({});
  });

  it("破坏者：模型为空 → 校验失败且不出任何请求", async () => {
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={null}
        options={OPTIONS}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => expect(screen.getByTestId("profile-error")).toBeInTheDocument());
    expect(apiMocks.post).not.toHaveBeenCalled();
  });

  it("创建 → createProfile + activateProfile（配置完即可用）+ onChanged", async () => {
    apiMocks.post.mockResolvedValue({});
    const onChanged = vi.fn();
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={null}
        options={OPTIONS}
        onChanged={onChanged}
      />,
    );
    fireEvent.change(screen.getByTestId("profile-name"), { target: { value: "深寻主力" } });
    fireEvent.change(screen.getByTestId("profile-model"), { target: { value: "deepseek-v4-pro" } });
    fireEvent.change(screen.getByTestId("profile-api-key"), { target: { value: "sk-new-key" } });
    fireEvent.click(screen.getByTestId("profile-save"));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(apiMocks.post).toHaveBeenCalledTimes(2); // create + activate
    const createBody = apiMocks.post.mock.calls[0][1];
    expect(createBody.model).toBe("deepseek-v4-pro");
    expect(createBody.api_key).toBe("sk-new-key");
  });

  it("保存成功即广播模型版本（会话框控制条重取，不依赖调用方透传）", async () => {
    apiMocks.post.mockResolvedValue({});
    const before = useUiStore.getState().modelsVersion;
    const onChanged = vi.fn();
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={null}
        options={OPTIONS}
        onChanged={onChanged}
      />,
    );
    fireEvent.change(screen.getByTestId("profile-name"), { target: { value: "深寻主力" } });
    fireEvent.change(screen.getByTestId("profile-model"), { target: { value: "deepseek-v4-pro" } });
    fireEvent.change(screen.getByTestId("profile-api-key"), { target: { value: "sk-new-key" } });
    fireEvent.click(screen.getByTestId("profile-save"));

    await waitFor(() =>
      expect(useUiStore.getState().modelsVersion).toBe(before + 1),
    );
  });

  it("破坏者：编辑态回显的是掩码 key —— 未改动保存时不得把掩码当新 key 发出", async () => {
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={PROFILE}
        options={OPTIONS}
        onChanged={vi.fn()}
      />,
    );
    expect((screen.getByTestId("profile-api-key") as HTMLInputElement).value).toBe("sk-***5678");
    fireEvent.change(screen.getByTestId("profile-name"), { target: { value: "改名" } });
    fireEvent.click(screen.getByTestId("profile-save"));

    await waitFor(() => expect(apiMocks.put).toHaveBeenCalled());
    const [, putBody] = apiMocks.put.mock.calls[0];
    expect(putBody.api_key).toBe("sk-***5678"); // 掩码原样上送，由后端识别并保留原 key
  });

  it("高级折叠区默认收起；展开后可填 thinking_mode 并入 extra 上送", async () => {
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={null}
        options={OPTIONS}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("profile-thinking-mode")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("profile-advanced-toggle"));
    expect(screen.getByTestId("profile-thinking-mode")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("profile-model"), { target: { value: "deepseek-v4-pro" } });
    fireEvent.change(screen.getByTestId("profile-thinking-mode"), { target: { value: "high" } });
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => expect(apiMocks.post).toHaveBeenCalled());
    expect(apiMocks.post.mock.calls[0][1].extra.thinking_mode).toBe("high");
  });

  it("破坏者：本弹窗只做「新增/编辑配置」——编辑态不得出现 删除 / 设为当前（2026-09-24 用户拍板）", () => {
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={PROFILE}
        options={OPTIONS}
        onChanged={vi.fn()}
      />,
    );
    // 弹窗内不得提供删除与切换激活（这两项移去「设置」里做）
    expect(screen.queryByTestId("profile-delete")).not.toBeInTheDocument();
    expect(screen.queryByTestId("profile-activate")).not.toBeInTheDocument();
    // 但保存（编辑）必须保留
    expect(screen.getByTestId("profile-save")).toBeInTheDocument();
  });

  it("官方 provider 编辑态不出现自定义输入框（自定义入口仅对清单外 provider 暴露）", () => {
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={PROFILE}
        options={OPTIONS}
        onChanged={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("profile-provider-custom")).not.toBeInTheDocument();
  });

  it("自定义 provider：编辑态回显输入框 + 保存透传自定义标识（不做官方清单过滤）", async () => {
    const onChanged = vi.fn();
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={{ ...PROFILE, provider: "openrouter", model: "x-ai/grok-4" }}
        options={OPTIONS}
        onChanged={onChanged}
      />,
    );
    const custom = screen.getByTestId("profile-provider-custom") as HTMLInputElement;
    expect(custom.value).toBe("openrouter");
    fireEvent.change(screen.getByTestId("profile-model"), { target: { value: "x-ai/grok-4" } });
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => expect(apiMocks.put).toHaveBeenCalled());
    expect(apiMocks.put.mock.calls[0][1].provider).toBe("openrouter");
  });

  it("布局一致性（2026-09-24 真机反馈）：下拉与输入框等宽 + 弹窗限高内滚 + 页脚弱化", () => {
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={null}
        options={OPTIONS}
        onChanged={vi.fn()}
      />,
    );
    // ① 供应商下拉必须撑满（与下方 Input 的 w-full 一致，否则长度不齐）
    expect(screen.getByTestId("profile-provider").className).toContain("w-full");
    // ② 弹窗限高 + **滚动收在字段区内层**（三轮反馈：整层滚动会让滚动条
    // 贴着弹窗圆角边框 ⇒ 溢出观感；改内层滚动 + pr 留距，页头/页脚固定不滚）
    const dialog = screen.getByTestId("model-config-dialog");
    expect(dialog.className).toContain("max-h-[85vh]");
    expect(dialog.className).not.toContain("overflow-y-auto");
    const fields = dialog.querySelector(".overflow-y-auto") as HTMLElement;
    expect(fields).not.toBeNull();
    expect(fields.className).toContain("pr-1.5");
    expect(fields.contains(screen.getByTestId("profile-save"))).toBe(false);
    // ③ 页脚不得有灰底（分区过重）；分隔线为最浅 hairline
    const footer = dialog.querySelector('[data-slot="dialog-footer"]') as HTMLElement;
    expect(footer.className).toContain("bg-transparent");
    expect(footer.className).not.toContain("bg-muted");
    expect(footer.className).toContain("border-chat-hairline");
    // ④ 展开高级后内容仍在弹窗内（不新增外层滚动容器）
    fireEvent.click(screen.getByTestId("profile-advanced-toggle"));
    expect(screen.getByTestId("profile-thinking-mode")).toBeInTheDocument();
    expect(document.querySelectorAll('[data-slot="dialog-content"]').length).toBe(1);
  });

  it("模型候选按字母序（2026-09-24 用户拍板：全段字母自动排序）", () => {
    const shuffled: ModelOption[] = [
      { name: "o4-mini", provider: "openai", supports_reasoning: true, reasoning_efforts: [] },
      { name: "gpt-4o", provider: "openai", supports_reasoning: false, reasoning_efforts: [] },
      {
        name: "deepseek-v4-pro",
        provider: "deepseek",
        supports_reasoning: true,
        reasoning_efforts: [],
      },
    ];
    render(
      <ModelConfigDialog
        open
        onOpenChange={vi.fn()}
        profile={null}
        options={shuffled}
        onChanged={vi.fn()}
      />,
    );
    const names = Array.from(
      document.querySelectorAll<HTMLOptionElement>("#profile-model-options option"),
    ).map((option) => option.value);
    expect(names).toEqual(["deepseek-v4-pro", "gpt-4o", "o4-mini"]);
  });
});
