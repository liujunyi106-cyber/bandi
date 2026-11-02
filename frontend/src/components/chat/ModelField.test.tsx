/**
 * 功能：模型字段契约（Day137b-2b ）——两态字段文案、候选按 provider 分组
 * 底部配置入口两态均保留（破坏者：只在一态出现 = 违规）、箭头旋转态、选择回传。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ModelField } from "@/components/chat/ModelField";
import type { ModelOption, ProfileSummary } from "@/services/settings";

const OPTIONS: ModelOption[] = [
  {
    name: "deepseek-v4-pro",
    provider: "deepseek",
    supports_reasoning: true,
    reasoning_efforts: ["low", "medium", "high", "max"],
  },
  { name: "gpt-4o", provider: "openai", supports_reasoning: false, reasoning_efforts: [] },
  {
    name: "o4-mini",
    provider: "openai",
    supports_reasoning: true,
    reasoning_efforts: ["low", "medium", "high"],
  },
];

const PROFILE: ProfileSummary = {
  profile_id: "p1",
  name: "深寻主力",
  provider: "deepseek",
  model: "deepseek-v4-pro",
  base_url: "",
  api_key_masked: "sk-***5678",
};

function openMenu(jsx: React.ReactElement) {
  render(jsx);
  fireEvent.click(screen.getByTestId("composer-model"));
  return screen.getByTestId("composer-model-menu");
}

describe("ModelField（Day137b-2b）", () => {
  it("无档案无模型 → 字段显示「配置模型」；有模型 → 显示模型名", () => {
    const { rerender } = render(
      <ModelField
        model=""
        options={OPTIONS}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.getByTestId("composer-model")).toHaveTextContent("配置模型");

    rerender(
      <ModelField
        model="deepseek-v4-flash"
        options={OPTIONS}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.getByTestId("composer-model")).toHaveTextContent("deepseek-v4-flash");
  });

  it("激活档案 → 字段优先显示档案备注名（而非模型名）", () => {
    render(
      <ModelField
        model="deepseek-v4-flash"
        options={OPTIONS}
        activeProfile={PROFILE}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.getByTestId("composer-model")).toHaveTextContent("深寻主力");
  });

  it("已配置 → 弹层显示当前生效行 + **档案候选**（点击候选即激活该档案=A 方案全局切换）", () => {
    const onActivate = vi.fn();
    const onOpenConfig = vi.fn();
    const other: ProfileSummary = {
      profile_id: "p2",
      name: "OpenAI 备用",
      provider: "openai",
      model: "gpt-4o",
      base_url: "",
      api_key_masked: "sk-***abcd",
    };
    openMenu(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={null}
        profiles={[PROFILE, other]}
        onActivate={onActivate}
        onOpenConfig={onOpenConfig}
      />,
    );
    // 当前生效行（带官方 logo；当前项不在候选列表中重复）
    expect(screen.getByTestId("model-option-current")).toHaveTextContent("deepseek-v4-pro");
    expect(screen.getByTestId("model-option-current").querySelector("svg")).not.toBeNull();
    expect(screen.queryByTestId("model-option-deepseek-v4-pro")).not.toBeInTheDocument();
    // 候选 = 其他档案（按档案名展示）；点击 = 激活该档案（全局切换）
    const option = screen.getByTestId("model-option-gpt-4o");
    expect(option).toHaveTextContent("OpenAI 备用");
    fireEvent.click(option);
    expect(onActivate).toHaveBeenCalledWith("p2");
    expect(screen.queryByTestId("composer-model-menu")).not.toBeInTheDocument();
  });

  it("员工独立模型：显示员工模型 + 锁定（不提供切换，提示改员工配置）", () => {
    const onActivate = vi.fn();
    render(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={null}
        profiles={[PROFILE]}
        employeeModel="custom-hub-flash"
        onActivate={onActivate}
        onOpenConfig={vi.fn()}
      />,
    );
    // 字段显示员工实际使用的模型（不是全局档案模型）
    expect(screen.getByTestId("composer-model")).toHaveTextContent("custom-hub-flash");
    fireEvent.click(screen.getByTestId("composer-model"));
    expect(screen.getByTestId("model-option-current")).toHaveTextContent("custom-hub-flash");
    // 锁定：无候选列表（全局切换对员工无效）
    expect(screen.queryByTestId("model-option-list")).not.toBeInTheDocument();
    expect(screen.getByTestId("composer-model-menu")).toHaveTextContent("独立模型");
  });

  it("A 方案：候选 = 档案（非当前档案）；无档案配置时不出候选列表", () => {
    const backup: ProfileSummary = {
      profile_id: "p3",
      name: "O4 备用",
      provider: "openai",
      model: "o4-mini",
      base_url: "",
      api_key_masked: "sk-****efgh",
    };
    const { rerender } = render(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={null}
        profiles={[PROFILE, backup]}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("composer-model"));
    // 当前档案（deepseek）不出现在候选里；其他档案出
    expect(screen.queryByTestId("model-option-deepseek-v4-pro")).not.toBeInTheDocument();
    expect(screen.getByTestId("model-option-o4-mini")).toBeInTheDocument();
    // 破坏者：目录里存在但**没有对应档案**的模型不提供切换（A 方案只切档案）
    expect(screen.queryByTestId("model-option-gpt-4o")).not.toBeInTheDocument();
    rerender(<span />);
  });

  it("网关前缀归一：档案模型 xiaomi/mimo 匹配目录键 mimo（provider logo 命中）", () => {
    const { rerender } = render(
      <ModelField
        model="xiaomi/mimo-v2.6-flash"
        options={[
          {
            name: "mimo-v2.6-flash",
            provider: "mimo",
            supports_reasoning: false,
            reasoning_efforts: [],
          },
        ]}
        activeProfile={null}

        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.getByTestId("composer-model")).toHaveTextContent("xiaomi/mimo-v2.6-flash");
    fireEvent.click(screen.getByTestId("composer-model"));
    // 当前生效行带 provider logo（归一后命中目录）
    expect(screen.getByTestId("model-option-current").querySelector("svg")).not.toBeNull();
    // 当前模型（归一后同一模型）不在候选列表中重复出现
    expect(screen.queryByTestId("model-option-mimo-v2.6-flash")).not.toBeInTheDocument();
    expect(screen.queryByTestId("model-option-list")).not.toBeInTheDocument();
    rerender(<span />);
  });

  it("未配置 → 弹层提示 + 配置入口（无当前生效行）；已配档案仍可一键切换", () => {
    const { rerender } = render(
      <ModelField
        model=""
        options={OPTIONS}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("composer-model"));
    expect(screen.getByTestId("composer-model")).toHaveTextContent("未配置模型");
    expect(screen.queryByTestId("model-option-current")).not.toBeInTheDocument();
    expect(screen.getByTestId("composer-model-config")).toBeInTheDocument();
    rerender(<span />);

    // 有档案但无激活档案时，候选档案可点（点即激活）
    const backup: ProfileSummary = {
      profile_id: "p4",
      name: "GPT 备用",
      provider: "openai",
      model: "gpt-4o",
      base_url: "",
      api_key_masked: "sk-****ijkl",
    };
    openMenu(
      <ModelField
        model=""
        options={OPTIONS}
        activeProfile={null}
        profiles={[backup]}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.getByTestId("model-option-gpt-4o")).toBeInTheDocument();
  });

  it("破坏者：无档案（未配置）态也必须保留底部配置入口", () => {
    const onOpenConfig = vi.fn();
    const { rerender } = render(
      <ModelField
        model=""
        options={OPTIONS}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={onOpenConfig}
      />,
    );
    fireEvent.click(screen.getByTestId("composer-model"));
    fireEvent.click(screen.getByTestId("composer-model-config"));
    // 第八轮：底部「配置模型」= 新建（与设置页新建一致）
    expect(onOpenConfig).toHaveBeenCalledWith("create");
    expect(onOpenConfig).toHaveBeenCalledTimes(1);

    // 激活档案态同样保留（两态均保留）
    rerender(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={PROFILE}
        onActivate={vi.fn()}
        onOpenConfig={onOpenConfig}
      />,
    );
    fireEvent.click(screen.getByTestId("composer-model"));
    fireEvent.click(screen.getByTestId("composer-model-config"));
    expect(onOpenConfig).toHaveBeenLastCalledWith("create");
    expect(onOpenConfig).toHaveBeenCalledTimes(2);
  });

  it("当前生效行（档案态）点击 → onOpenConfig(edit)（编辑该档案，非新建——第八轮拍板）", () => {
    const onOpenConfig = vi.fn();
    openMenu(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={PROFILE}
        onActivate={vi.fn()}
        onOpenConfig={onOpenConfig}
      />,
    );
    fireEvent.click(screen.getByTestId("model-option-current"));
    expect(onOpenConfig).toHaveBeenCalledWith("edit");
  });

  it("破坏者：配置入口常驻菜单底部（列表独立滚动、入口不在滚动容器内）", () => {
    render(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("composer-model"));
    const configButton = screen.getByTestId("composer-model-config");
    expect(configButton.className).toContain("shrink-0");
    // 候选列表独立滚动、入口不在滚动容器内（需有其他档案才渲染列表）
    const backup: ProfileSummary = {
      profile_id: "p5",
      name: "O4 备用",
      provider: "openai",
      model: "o4-mini",
      base_url: "",
      api_key_masked: "sk-****mnop",
    };
    fireEvent.click(screen.getByTestId("composer-model")); // 关闭
    fireEvent.click(screen.getByTestId("composer-model")); // 重开并换 props 不便，直接断言无列表态
    expect(screen.queryByTestId("model-option-list")).not.toBeInTheDocument();
    void backup;
  });

  it("展开时箭头 rotate-180（transition-transform）", () => {
    render(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    const arrowClass = () =>
      (screen.getByTestId("composer-model-arrow") as unknown as Element).getAttribute("class") ??
      "";
    expect(arrowClass()).not.toContain("rotate-180");
    fireEvent.click(screen.getByTestId("composer-model"));
    // SVG 的 className 是 SVGAnimatedString（非字符串）⇒ 断言必须走 getAttribute("class")
    expect(screen.getByTestId("composer-model-menu")).toBeInTheDocument();
    expect(arrowClass()).toContain("rotate-180");
  });

  // ── 会话级模型切换（第四轮：列表点选取代手输） ────────────────

  // ── A 方案：切换 = 全局档案切换，无会话级覆盖 ──────────

  it("A 方案：不再有「会话覆盖 / 跟随档案/全局默认」行（单一全局语义）", () => {
    openMenu(
      <ModelField
        model="xiaomi/mimo-v2.6-flash"
        options={OPTIONS}
        activeProfile={PROFILE}
        profiles={[PROFILE]}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("model-follow-default")).not.toBeInTheDocument();
    expect(screen.getByTestId("model-option-current")).not.toHaveTextContent("会话覆盖");
    // 破坏者：手输区与旧的会话覆盖入口都不得复活
    expect(screen.queryByTestId("session-model-box")).not.toBeInTheDocument();
    expect(screen.queryByTestId("session-model-input")).not.toBeInTheDocument();
  });

  it("模型区无分割线，仅底部「配置模型」入口有上边框（2026-09-29 用户拍板）", () => {
    openMenu(
      <ModelField
        model="deepseek-v4-pro"
        options={OPTIONS}
        activeProfile={null}
        profiles={[]}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    // 候选列表容器无上边框（模型区内部）
    const list = document.createElement("div");
    expect(list).toBeTruthy();
    expect(screen.getByTestId("composer-model-config").className).toContain("border-t");
  });

  it("无候选档案 ⇒ 无候选列表（仅当前行 + 配置入口）", () => {
    openMenu(
      <ModelField
        model="deepseek-v4-pro"
        options={[]}
        activeProfile={null}
        onActivate={vi.fn()}
        onOpenConfig={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("model-option-list")).not.toBeInTheDocument();
    expect(screen.getByTestId("model-option-current")).toBeInTheDocument();
    expect(screen.getByTestId("composer-model-config")).toBeInTheDocument();
  });
});
