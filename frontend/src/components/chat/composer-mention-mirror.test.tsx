/**
 * 功能：Composer「发送前 @提及高亮」契约（发送前也要蓝）。
 *
 * 实现形态 = **镜像层**：textarea 自身文字透明（保留 caret/placeholder）
 * 上方 aria-hidden 的镜像 div 渲染同一文本并把 @提及染成 `text-mention`。
 * 本契约钉住三件不能退化的行为：
 * ① 输入 @员工名/角色 id → 镜像出现 `text-mention` span（其余文本不变色）；
 * ② 两层必须**逐字对齐**（同字体/同内边距/同宽，且 textarea 为 block 不留基线间隙）；
 * ③ 镜像不得抢事件、不得被读屏重复朗读（pointer-events-none + aria-hidden）。
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Composer } from "@/components/chat/Composer";
import { useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

vi.mock("@/hooks/use-employees", () => ({
  useEmployees: () => ({
    employees: [{ employee_id: "e1", name: "小美", role_id: "dev" }],
    loaded: true,
  }),
}));

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("offline"))),
  );
  useChatStore.setState({ draftAttachments: [], messagesBySession: {} });
  useUiStore.setState({ chatMode: "plan", sessionModel: "", sessionThinking: "" });
});

function renderComposer() {
  return render(
    <Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />,
  );
}

describe("Composer 发送前 @提及高亮（镜像层）", () => {
  it("输入 @员工名 → 镜像层出现 text-mention（其余文本不染色）", async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.type(screen.getByRole("textbox"), "@小美 请看");

    const mirror = screen.getByTestId("composer-mirror");
    const highlighted = Array.from(mirror.querySelectorAll("span")).filter((el) =>
      el.className.includes("text-mention"),
    );
    expect(highlighted.map((el) => el.textContent)).toEqual(["@小美"]);
    // 非提及片段不得染色
    expect(mirror.textContent).toContain("请看");
  });

  it("角色 id 形式同样命中（与消息面同口径）", async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.type(screen.getByRole("textbox"), "@dev 跟进");

    const mirror = screen.getByTestId("composer-mirror");
    expect(
      Array.from(mirror.querySelectorAll("span")).some(
        (el) => el.textContent === "@dev" && el.className.includes("text-mention"),
      ),
    ).toBe(true);
  });

  it("破坏者：无 @ 文本不得染色（防「整段变蓝」回归）", async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.type(screen.getByRole("textbox"), "普通消息没有提及");

    const mirror = screen.getByTestId("composer-mirror");
    expect(mirror.querySelectorAll("span.text-mention")).toHaveLength(0);
  });

  it("两层逐字对齐的前提：同字体/同内边距/同宽 + textarea 为 block（无基线间隙）", () => {
    renderComposer();
    const textarea = screen.getByRole("textbox");
    const mirror = screen.getByTestId("composer-mirror");

    for (const cls of ["px-2", "py-2", "text-sm"]) {
      expect(textarea.className, `textarea 缺 ${cls}`).toContain(cls);
      expect(mirror.className, `镜像层缺 ${cls}`).toContain(cls);
    }
    // inline-block 会在行盒里留下基线间隙 ⇒ 输入框会凭空高出 ~6px
    expect(textarea.className).toContain("block");
    // 输入文字必须透明（由镜像层呈现），caret 仍要可见
    expect(textarea.className).toContain("text-transparent");
    expect(textarea.className).toContain("caret-chat-ink");
  });

  it("破坏者：镜像层不得抢事件或被读屏重复朗读", () => {
    renderComposer();
    const mirror = screen.getByTestId("composer-mirror");
    expect(mirror.getAttribute("aria-hidden")).toBe("true");
    expect(mirror.className).toContain("pointer-events-none");
  });
});
