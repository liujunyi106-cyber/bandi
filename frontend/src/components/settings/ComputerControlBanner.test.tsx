/**
 * 功能：桌面控制常驻提示条测试——未启用不渲染、启用后可一键关闭、事件可即时同步。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  COMPUTER_USE_CHANGED_EVENT,
  ComputerControlBanner,
} from "@/components/settings/ComputerControlBanner";

const settingsApi = {
  computerStatus: vi.fn(),
  setComputerEnabled: vi.fn(),
};

vi.mock("@/services/settings", () => ({
  settingsApi: {
    computerStatus: () => settingsApi.computerStatus(),
    setComputerEnabled: (enabled: boolean, confirm: boolean) =>
      settingsApi.setComputerEnabled(enabled, confirm),
  },
}));

describe("ComputerControlBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("未启用时不渲染", async () => {
    settingsApi.computerStatus.mockResolvedValue({ enabled: false });
    render(<ComputerControlBanner />);
    await waitFor(() => expect(settingsApi.computerStatus).toHaveBeenCalled());
    expect(screen.queryByTestId("computer-use-banner")).not.toBeInTheDocument();
  });

  it("启用后显示并能一键关闭", async () => {
    settingsApi.computerStatus.mockResolvedValue({ enabled: true });
    settingsApi.setComputerEnabled.mockResolvedValue({ ok: true, enabled: false });
    render(<ComputerControlBanner />);

    expect(await screen.findByTestId("computer-use-banner")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("computer-use-banner-disable"));
    await waitFor(() => expect(settingsApi.setComputerEnabled).toHaveBeenCalledWith(false, false));
    await waitFor(() =>
      expect(screen.queryByTestId("computer-use-banner")).not.toBeInTheDocument(),
    );
  });

  it("设置页开关事件即时同步（不重新拉接口）", async () => {
    settingsApi.computerStatus.mockResolvedValue({ enabled: false });
    render(<ComputerControlBanner />);
    await waitFor(() => expect(settingsApi.computerStatus).toHaveBeenCalledTimes(1));

    window.dispatchEvent(new CustomEvent(COMPUTER_USE_CHANGED_EVENT, { detail: { enabled: true } }));
    expect(await screen.findByTestId("computer-use-banner")).toBeInTheDocument();
    expect(settingsApi.computerStatus).toHaveBeenCalledTimes(1);
  });
});
