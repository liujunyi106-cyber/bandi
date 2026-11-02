/**
 * 功能：Composer 行为测试（模式切换器 / 发送与转向分派）。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/chat/Composer";
import { useUiStore } from "@/stores/ui-store";

describe("Composer", () => {
  beforeEach(() => {
    useUiStore.setState({ chatMode: "plan" });
  });

  it("模式切换器：默认计划（未选中灰）→ 切换后执行（选中实心）", () => {
    render(<Composer busy={false} onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    const toggle = screen.getByTestId("composer-mode-toggle");
    expect(toggle.getAttribute("data-mode")).toBe("plan");
    expect(toggle).toHaveTextContent("计划");
    // 未选中（灰线框；悬浮填充是 bg-primary/5，故判据用「实心三连」而非裸 bg-primary）
    expect(toggle.className).not.toContain("border-primary bg-primary text-primary-foreground");

    fireEvent.click(toggle);

    expect(useUiStore.getState().chatMode).toBe("execute");
    expect(toggle.getAttribute("data-mode")).toBe("execute");
    expect(toggle).toHaveTextContent("执行");
    expect(toggle.className).toContain("border-primary bg-primary text-primary-foreground"); // 选中（实心主色）
  });

  it("布局：输入栏在上、功能栏（模式 + 发送）在下", () => {
    render(<Composer busy={false} onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    const composer = screen.getByTestId("composer");
    const textarea = screen.getByRole("textbox");
    const toggle = screen.getByTestId("composer-mode-toggle");
    // 功能栏容器同时包含模式圆圈与发送按钮，且位于输入栏之后（DOM 顺序）
    const position = textarea.compareDocumentPosition(toggle);
    expect(position & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(composer.querySelectorAll("textarea")).toHaveLength(1);
    expect(toggle.querySelector("span")).toBeNull(); // 圆圈外无附加字段
  });

  it("Shift+Tab 快捷切换（输入框聚焦）；无 Shift 的 Tab 不切换", () => {
    render(<Composer busy={false} onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    const textarea = screen.getByRole("textbox");

    fireEvent.keyDown(textarea, { key: "Tab", shiftKey: true });
    expect(useUiStore.getState().chatMode).toBe("execute");

    fireEvent.keyDown(textarea, { key: "Tab", shiftKey: true });
    expect(useUiStore.getState().chatMode).toBe("plan");

    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(useUiStore.getState().chatMode).toBe("plan");
  });

  it("健壮性：焦点在模式按钮上按 Shift+Tab 也切换（容器级捕获）", () => {
    render(<Composer busy={false} onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    fireEvent.keyDown(screen.getByTestId("composer-mode-toggle"), { key: "Tab", shiftKey: true });
    expect(useUiStore.getState().chatMode).toBe("execute");
  });

  it("再次点击切回计划模式", () => {
    useUiStore.setState({ chatMode: "execute" });
    render(<Composer busy={false} onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    fireEvent.click(screen.getByTestId("composer-mode-toggle"));
    expect(useUiStore.getState().chatMode).toBe("plan");
  });

  it("空闲发送走 onSend；忙碌发送走 onSteer", () => {
    const onSend = vi.fn();
    const onSteer = vi.fn();
    const { unmount } = render(
      <Composer busy={false} onSend={onSend} onSteer={onSteer} onStop={vi.fn()} />,
    );
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "你好" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("你好");
    unmount();

    render(<Composer busy onSend={onSend} onSteer={onSteer} onStop={vi.fn()} />);
    const busyTextarea = screen.getByRole("textbox");
    fireEvent.change(busyTextarea, { target: { value: "转向" } });
    fireEvent.keyDown(busyTextarea, { key: "Enter" });
    expect(onSteer).toHaveBeenCalledWith("转向");
  });

  it("停止键：busy 时发送键原位变停止（方形小圆角实心），点击两次确认停止", () => {
    const onStop = vi.fn();
    render(<Composer busy onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);

    // 发送键被停止键取代（不并列两个按钮）
    expect(screen.queryByTitle("发送")).not.toBeInTheDocument();
    const stop = screen.getByTestId("composer-stop");
    expect(stop.className).toContain("rounded-full");
    // 中断 icon 用方形（lucide Square 自带 rx=2 小圆角）实心
    expect(stop.querySelector(".lucide-square")).toBeTruthy();
    expect(stop.querySelector(".lucide-square")?.getAttribute("class")).toContain("fill-current");
    // 外圆黑色（此前 primary 蓝底）——内部方块随文字色为白
    expect(stop.className).toContain("bg-chat-ink");

    fireEvent.click(stop); // 第一次：待触发（双触发），不停止
    expect(onStop).not.toHaveBeenCalled();
    // 取消「再点一次停止」确认态——title 恒为普通「停止」
    // 待触发只用一圈轻描边示意（原实现的破坏性红态 bg-destructive 已移除）
    expect(screen.getByTestId("composer-stop").getAttribute("title")).toBe("停止");
    expect(screen.getByTestId("composer-stop").className).not.toContain("bg-destructive");
    expect(screen.getByTestId("composer-stop").className).toContain("ring-2");

    fireEvent.click(screen.getByTestId("composer-stop")); // 第二次：停止
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("Esc 直接停止（busy）", () => {
    const onStop = vi.fn();
    render(<Composer busy onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
    // Esc 与停止按钮同属双触发，单次 Esc 不停
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    expect(onStop).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("Esc 全局生效：焦点不在输入框（页面任意处）也停止（2026-09-24 真机反馈）", () => {
    const onStop = vi.fn();
    render(<Composer busy onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onStop).not.toHaveBeenCalled();
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("非 busy 时 Esc 不触发停止（全局监听守卫）", () => {
    const onStop = vi.fn();
    render(<Composer busy={false} onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(onStop).not.toHaveBeenCalled();
  });

  it("above 决策区：渲染在输入框上方且不改变输入框圆角（2026-09-25 用户拍板）", () => {
    render(
      <Composer
        busy={false}
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
        above={<div data-testid="above-demo">决策卡</div>}
      />,
    );
    const box = screen.getByTestId("composer-box");
    expect(box.className).toContain("rounded-xl");
    expect(box.className).not.toContain("rounded-t-none");
    // above 内容在输入框之前（DOM 序）
    const above = screen.getByTestId("above-demo");
    expect((above.compareDocumentPosition(box) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(
      true,
    );
  });

  it("showMode=false：无模式控件且 Shift+Tab 不切换（派单视图）", () => {
    render(
      <Composer
        busy={false}
        showMode={false}
        placeholder="给这位员工发消息…"
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("composer-mode-toggle")).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("给这位员工发消息…")).toBeInTheDocument();

    const before = useUiStore.getState().chatMode;
    fireEvent.keyDown(screen.getByRole("textbox"), { key: "Tab", shiftKey: true });
    expect(useUiStore.getState().chatMode).toBe(before);
  });
});
