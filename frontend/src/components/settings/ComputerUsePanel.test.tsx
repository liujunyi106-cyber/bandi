/**
 * 功能：Computer Use 面板测试——开关需二次确认、未装环境不可启用、
 * 安装失败回显原因、解释器保存走真实端点、启用后有一键关闭横幅。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ComputerUsePanel } from "@/components/settings/SettingsPanels";

const settingsApi = {
  computerStatus: vi.fn(),
  installComputerRuntime: vi.fn(),
  setComputerInterpreter: vi.fn(),
  setComputerEnabled: vi.fn(),
};

vi.mock("@/services/settings", () => ({
  settingsApi: {
    computerStatus: () => settingsApi.computerStatus(),
    installComputerRuntime: (interpreter: string) =>
      settingsApi.installComputerRuntime(interpreter),
    setComputerInterpreter: (path: string) => settingsApi.setComputerInterpreter(path),
    setComputerEnabled: (enabled: boolean, confirm: boolean) =>
      settingsApi.setComputerEnabled(enabled, confirm),
  },
}));

function status(overrides: Record<string, unknown> = {}) {
  return {
    enabled: false,
    venv_dir: "C:/Users/x/.bandi/runtime/computer-venv",
    venv_exists: true,
    python: "C:/Users/x/.bandi/runtime/computer-venv/Scripts/python.exe",
    python_version: "3.12.10",
    deps_installed: true,
    deps: { mss: true, pyautogui: true, PIL: true, pyperclip: true },
    missing_packages: [],
    probe_error: "",
    screen: { width: 1920, height: 1080 },
    interpreter: {
      path: "C:/Python312/python.exe",
      version: "3.12.10",
      source: "auto",
      candidates: [
        { path: "C:/Python312/python.exe", version: "3.12.10" },
        { path: "C:/py311/python.exe", version: "3.11.9" },
      ],
    },
    install: { phase: "ready", progress: 100, message: "运行时就绪", log: [] },
    safe_mode: true,
    platform: "Windows",
    helper: "agent_script.py",
    ...overrides,
  };
}

describe("ComputerUsePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("开启必须二次确认，确认后调用端点并显示横幅", async () => {
    settingsApi.computerStatus.mockResolvedValue(status());
    settingsApi.setComputerEnabled.mockResolvedValue({
      ok: true,
      enabled: true,
      status: status({ enabled: true }),
    });
    render(<ComputerUsePanel />);

    const toggle = await screen.findByTestId("cu-enable");
    await waitFor(() => expect(toggle).toHaveAttribute("aria-disabled", "false"));

    await userEvent.click(toggle);
    expect(settingsApi.setComputerEnabled).not.toHaveBeenCalled();
    expect(await screen.findByTestId("cu-confirm")).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("cu-confirm-yes"));
    await waitFor(() => expect(settingsApi.setComputerEnabled).toHaveBeenCalledWith(true, true));
    expect(await screen.findByTestId("cu-banner")).toBeInTheDocument();
  });

  it("未装依赖时开关禁用且不发起请求", async () => {
    settingsApi.computerStatus.mockResolvedValue(
      status({
        deps_installed: false,
        venv_exists: false,
        missing_packages: ["pyautogui"],
        deps: { mss: true, pyautogui: false, PIL: true, pyperclip: true },
        install: { phase: "idle", progress: 0, log: [] },
      }),
    );
    render(<ComputerUsePanel />);

    const toggle = await screen.findByTestId("cu-enable");
    await waitFor(() => expect(toggle).toHaveAttribute("aria-disabled", "true"));
    await userEvent.click(toggle);
    expect(screen.queryByTestId("cu-confirm")).not.toBeInTheDocument();
    expect(settingsApi.setComputerEnabled).not.toHaveBeenCalled();
    expect(screen.getByTestId("cu-deps")).toHaveTextContent("✗ pyautogui");
  });

  it("安装失败回显后端原因与日志", async () => {
    settingsApi.computerStatus.mockResolvedValue(
      status({
        deps_installed: false,
        install: {
          phase: "failed",
          progress: 0,
          error: "RuntimeError: ERROR: No matching distribution found for pyautogui",
          log: ["# 解释器: C:/Python312/python.exe", "ERROR: No matching distribution"],
        },
      }),
    );
    render(<ComputerUsePanel />);

    expect(await screen.findByTestId("cu-progress")).toBeInTheDocument();
    expect(
      screen.getByText(/No matching distribution found for pyautogui/),
    ).toBeInTheDocument();
  });

  it("显示屏幕分辨率与缩放比（物理像素口径）", async () => {
    settingsApi.computerStatus.mockResolvedValue(status({ scale: 1.25 }));
    render(<ComputerUsePanel />);
    // 破坏者：不显示缩放 ⇒ 用户在 125% 屏上不知道模型看到的是物理像素
    expect(await screen.findByTestId("cu-screen")).toHaveTextContent("1920×1080 · 125%");
  });

  it("应用解释器走真实端点（去空格）", async () => {
    settingsApi.computerStatus.mockResolvedValue(status());
    settingsApi.setComputerInterpreter.mockResolvedValue({
      ok: true,
      interpreter: { path: "C:/py311/python.exe", version: "3.11.9" },
    });
    render(<ComputerUsePanel />);

    const input = await screen.findByTestId("cu-interpreter-input");
    await userEvent.clear(input);
    await userEvent.type(input, "  C:/py311/python.exe  ");
    await userEvent.click(screen.getByTestId("cu-interpreter-apply"));

    await waitFor(() =>
      expect(settingsApi.setComputerInterpreter).toHaveBeenCalledWith("C:/py311/python.exe"),
    );
  });

  it("启用后一键关闭", async () => {
    settingsApi.computerStatus.mockResolvedValue(status({ enabled: true }));
    settingsApi.setComputerEnabled.mockResolvedValue({
      ok: true,
      enabled: false,
      status: status({ enabled: false }),
    });
    render(<ComputerUsePanel />);

    await userEvent.click(await screen.findByTestId("cu-banner-close"));
    await waitFor(() => expect(settingsApi.setComputerEnabled).toHaveBeenCalledWith(false, false));
    await waitFor(() => expect(screen.queryByTestId("cu-banner")).not.toBeInTheDocument());
  });

  it("安装环境把当前解释器一并提交", async () => {
    settingsApi.computerStatus.mockResolvedValue(
      status({ deps_installed: false, install: { phase: "idle", progress: 0, log: [] } }),
    );
    settingsApi.installComputerRuntime.mockResolvedValue({
      started: true,
      already_running: false,
      state: { phase: "preparing", progress: 5 },
    });
    render(<ComputerUsePanel />);

    await userEvent.click(await screen.findByTestId("cu-install"));
    await waitFor(() =>
      expect(settingsApi.installComputerRuntime).toHaveBeenCalledWith("C:/Python312/python.exe"),
    );
  });
});
