/**
 * 功能：停止 = 双触发。
 *
 * 背景：原实现是「点第一下 → 按钮变红 + title tooltip 说『再点一次停止』」。
 * 该交互等同于坏掉——唯一提示藏在悬停 tooltip 里，按钮本体仍是个方块 icon
 * 首击在视觉上毫无变化，用户会判定「停止按钮没用」并放弃。
 * 故取消确认态，改为**双触发**：连点两次停止按钮、或连按两次 Esc 才真正 onStop。
 *
 * 本文件钉「行为」：单次触发绝不调 onStop；窗口内第二次才调；超窗自动失效。
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
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

describe("停止双触发", () => {
  it("单次点击不停止（首击不得等同于「按钮坏了」）", async () => {
    const { onStop, user, stop } = setup();
    await user.click(stop);
    expect(onStop).not.toHaveBeenCalled();
  });

  it("窗口内第二次点击才真正停止，且只调一次", async () => {
    const { onStop, user, stop } = setup();
    await user.click(stop);
    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("超出双触发窗口后重新武装（不残留上一次的待触发态）", async () => {
    const { onStop, user, stop } = setup();
    await user.click(stop);
    // 等过 STOP_DOUBLE_TAP_WINDOW_MS(1500ms) → 待触发态应自动解除
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 1700));
    });
    await user.click(stop);
    expect(onStop).not.toHaveBeenCalled();

    await user.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("Esc 单次不停、连按两次才停", async () => {
    const { onStop, user } = setup();
    const box = screen.getByRole("textbox");
    box.focus();
    await user.keyboard("{Escape}");
    expect(onStop).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("回归护栏：/stop 斜杠命令是明确意图，单次即停（不走双触发）", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
    await user.type(screen.getByRole("textbox"), "/stop{Enter}");
    expect(onStop).toHaveBeenCalledTimes(1);
  });
});

describe("群聊员工任务执行期的停止（teamRunning，非本会话流）", () => {
  it("teamRunning 时显示停止键，连按两次 Esc 触发停止", async () => {
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
    const box = screen.getByRole("textbox");
    box.focus();
    await user.keyboard("{Escape}");
    expect(onStop).not.toHaveBeenCalled();
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
