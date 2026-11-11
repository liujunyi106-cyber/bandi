/**
 * 功能：停止 = 单次触发。
 *
 * 助理、员工私聊、群聊统一：单击停止按钮或单次 Esc 立即 onStop。
 * 具体本地收尾与服务端 abort/cancel 由父层按会话类型执行。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/chat/Composer";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("offline"))),
  );
  useChatStore.setState({ draftAttachments: [], messagesBySession: {} });
  useUiStore.setState({ chatMode: "plan", sessionModel: "", sessionThinking: "" });
});

function setup() {
  const onStop = vi.fn();
  const user = userEvent.setup();
  render(<Composer busy onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
  return { onStop, user, stop: screen.getByTestId("composer-stop") };
}

describe("停止单次触发", () => {
  it("单击停止按钮立即停止", async () => {
    const { onStop, user, stop } = setup();
    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("单次 Esc 立即停止", async () => {
    const { onStop, user } = setup();
    screen.getByRole("textbox").focus();
    await user.keyboard("{Escape}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("全局 Esc（焦点不在输入框）也单次停止", async () => {
    const { onStop, user } = setup();
    await user.keyboard("{Escape}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("/stop 斜杠命令单次停止", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
    await user.type(screen.getByRole("textbox"), "/stop{Enter}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});

describe("群聊员工任务执行期的停止（teamRunning，非本会话流）", () => {
  it("teamRunning 时显示停止键，单次 Esc 触发停止", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    render(
      <Composer
        busy={false}
        teamRunning
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={onStop}
      />,
    );
    expect(screen.getByTestId("composer-stop")).toBeInTheDocument();
    screen.getByRole("textbox").focus();
    await user.keyboard("{Escape}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("teamRunning 结束（false）后回到发送键", () => {
    const { rerender } = render(
      <Composer busy={false} teamRunning onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />,
    );
    expect(screen.getByTestId("composer-stop")).toBeInTheDocument();
    rerender(
      <Composer
        busy={false}
        teamRunning={false}
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("composer-stop")).not.toBeInTheDocument();
  });
});
