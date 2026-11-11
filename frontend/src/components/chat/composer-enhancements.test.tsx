/**
 * 功能：Day137b-2 前端行为测试——Composer 增强（⊕/@/斜杠/附件发送）+ 附件 chips + 解析。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AttachmentChips } from "@/components/chat/AttachmentChips";
import { Composer } from "@/components/chat/Composer";
import { MessageItem } from "@/components/chat/MessageItem";
import { formatSize } from "@/lib/attachments";
import { parseEntryAttachments, useChatStore } from "@/stores/chat-store";
import { useUiStore } from "@/stores/ui-store";

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      // @ 面板员工来源（mentionRoleIds 过滤测试用；outsider = 项目外）
      employees: vi.fn(async () => ({
        employees: [
          { employee_id: "e1", name: "小美", role_id: "dev" },
          { employee_id: "e2", name: "阿强", role_id: "ops" },
          { employee_id: "e3", name: "外部人", role_id: "outsider" },
        ],
      })),
    },
  };
});

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("offline"))),
  );
  useChatStore.setState({ draftAttachments: [], messagesBySession: {} });
  useUiStore.setState({ chatMode: "plan", sessionModel: "", sessionThinking: "" });
});

describe("Composer 增强（Day137b-2）", () => {
  it("⊕ 菜单：三项（添加文件/选择团队/@引用）", async () => {
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.click(screen.getByTestId("composer-plus"));
    expect(screen.getByTestId("composer-plus-menu")).toBeInTheDocument();
    expect(screen.getByTestId("composer-plus-attach")).toBeInTheDocument();
    expect(screen.getByTestId("composer-plus-team")).toBeInTheDocument();
    expect(screen.getByTestId("composer-plus-quote")).toBeInTheDocument();
  });

  it("⊕ 悬浮/选中：加号颜色不变，改为圆圈内部填充（与多选卡同底色；2026-10-01 用户拍板）", async () => {
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    const plus = screen.getByTestId("composer-plus");
    const classes = plus.className;

    // 悬浮：填充 = 多选卡卡片底（primary/5），不得再加强描边/改加号颜色
    expect(classes).toContain("hover:bg-primary/5");
    expect(classes).not.toContain("hover:border-primary");
    expect(classes).not.toContain("hover:text-chat-ink");
    // 静默态加号保持 muted，不随悬浮变色
    expect(classes).toContain("text-chat-muted");

    // 选中（菜单展开）：同样走内部填充，而非另一种高亮
    await user.click(plus);
    expect(screen.getByTestId("composer-plus").className).toContain("bg-primary/5");
    expect(screen.getByTestId("composer-plus")).toHaveAttribute("aria-expanded", "true");
  });

  it("模式切换圆圈（计划态）悬浮与 ⊕ 同规则：内部填充而非描边加强", () => {
    render(<Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    const toggle = screen.getByTestId("composer-mode-toggle");
    const classes = toggle.className;
    expect(classes).toContain("hover:bg-primary/5");
    expect(classes).not.toContain("hover:border-primary");
    expect(classes).not.toContain("hover:text-chat-ink");
  });

  it("斜杠面板：输入 / 触发 + 执行 /plan 切换模式", async () => {
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.type(screen.getByRole("textbox"), "/");
    expect(await screen.findByTestId("composer-slash-panel")).toBeInTheDocument();
    await user.click(screen.getByTestId("slash-item-plan"));
    expect(useUiStore.getState().chatMode).toBe("plan");
  });

  it("@ 触发引用菜单（空数据空态兜底）", async () => {
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.type(screen.getByRole("textbox"), "@");
    expect(await screen.findByTestId("composer-mentions")).toBeInTheDocument();
  });

  it("发送携带已上传附件（草稿 done 态 → onSend 第二参 + 清草稿）", async () => {
    useChatStore.setState({
      draftAttachments: [{ upload_id: "u1", name: "报告.md", size: 12, status: "done" }],
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={onSend} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.type(screen.getByRole("textbox"), "看附件{Enter}");
    expect(onSend).toHaveBeenCalledWith("看附件", [{ upload_id: "u1", name: "报告.md", size: 12 }]);
    expect(useChatStore.getState().draftAttachments).toHaveLength(0);
  });

  it("仅图片无文字也能发送（附件单独构成一条消息）", async () => {
    useChatStore.setState({
      draftAttachments: [
        { upload_id: "img", name: "cat.png", size: 9, status: "done", project_id: "p1" },
      ],
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={onSend} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.click(screen.getByTestId("composer-send"));
    expect(onSend).toHaveBeenCalledWith("", [
      { upload_id: "img", name: "cat.png", size: 9, project_id: "p1" },
    ]);
  });

  it("非增强模式不显示 ⊕（Dm/Group 派单视图）", () => {
    render(
      <Composer
        busy={false}
        showMode={false}
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("composer-plus")).not.toBeInTheDocument();
  });

  it("attachmentsEnabled（单聊/群聊派单——轮三-5）：⊕ 仅「添加文件」项；无模式/控制条", async () => {
    const user = userEvent.setup();
    render(
      <Composer
        busy={false}
        showMode={false}
        attachmentsEnabled
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    expect(screen.getByTestId("composer-plus")).toBeInTheDocument();
    await user.click(screen.getByTestId("composer-plus"));
    expect(screen.getByTestId("composer-plus-attach")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-plus-team")).not.toBeInTheDocument();
    expect(screen.queryByTestId("composer-plus-quote")).not.toBeInTheDocument();
    expect(screen.queryByTestId("composer-mode-toggle")).not.toBeInTheDocument();
  });

  it("attachmentsEnabled：草稿 done 附件随发送进 onSend 第二参", async () => {
    useChatStore.setState({
      draftAttachments: [{ upload_id: "u9", name: "r.md", size: 3, status: "done" }],
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(
      <Composer
        busy={false}
        showMode={false}
        attachmentsEnabled
        onSend={onSend}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    await user.type(screen.getByRole("textbox"), "发你{Enter}");
    expect(onSend).toHaveBeenCalledWith("发你", [{ upload_id: "u9", name: "r.md", size: 3 }]);
  });

  it("busy + 已上传附件：不发送不转向、附件保留（137b-3 修复——原实现静默清草稿丢文件）", async () => {
    useChatStore.setState({
      draftAttachments: [{ upload_id: "u1", name: "报告.md", size: 12, status: "done" }],
    });
    const onSend = vi.fn();
    const onSteer = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy enhanced onSend={onSend} onSteer={onSteer} onStop={vi.fn()} />);
    await user.type(screen.getByRole("textbox"), "带附件转向{Enter}");
    expect(onSteer).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(useChatStore.getState().draftAttachments).toHaveLength(1);
  });

  it("上传中禁止发送：不清草稿不静默丢附件（137b-3 修复）", async () => {
    useChatStore.setState({
      draftAttachments: [{ upload_id: "u2", name: "上传中.md", size: 3, status: "uploading" }],
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={onSend} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.type(screen.getByRole("textbox"), "现在发{Enter}");
    expect(onSend).not.toHaveBeenCalled();
    expect(useChatStore.getState().draftAttachments).toHaveLength(1);
  });

  it("Esc：busy 且斜杠弹层无匹配（不可见）时仍能停止（137b-3 修复——原守卫按 flag 吞掉）", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={onStop} />);
    await user.type(screen.getByRole("textbox"), "/zzz无此命令");
    expect(screen.queryByTestId("composer-slash-panel")).not.toBeInTheDocument();
    // 单次 Esc 直接停止
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onStop).toHaveBeenCalled();
  });

  it("其他会话流式回复中：本会话仍可发送（按会话流槽）", async () => {
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(
      <Composer
        busy={false}
        enhanced
        onSend={onSend}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    await user.type(screen.getByRole("textbox"), "跨会话发送{Enter}");
    expect(onSend).toHaveBeenCalled();
  });

  it("发送后 error 附件保留供用户处理（137b-3 修复——原实现全清静默丢）", async () => {
    useChatStore.setState({
      draftAttachments: [
        { upload_id: "u3", name: "坏了.md", size: 1, status: "error" },
        { upload_id: "u4", name: "好的.md", size: 2, status: "done" },
      ],
    });
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={onSend} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.type(screen.getByRole("textbox"), "发送{Enter}");
    expect(onSend).toHaveBeenCalled();
    const remaining = useChatStore.getState().draftAttachments;
    expect(remaining).toHaveLength(1);
    expect(remaining[0].status).toBe("error");
  });
});

describe("附件 chips 与解析（Day137b-2）", () => {
  it("formatSize 三档（B/KB/MB）", () => {
    expect(formatSize(12)).toBe("12 B");
    expect(formatSize(2048)).toBe("2.0 KB");
    expect(formatSize(3 * 1024 * 1024)).toBe("3.0 MB");
  });

  it("AttachmentChips 三态渲染（uploading/error/done）", () => {
    render(
      <AttachmentChips
        attachments={[
          { upload_id: "a", name: "x.md", size: 1, status: "uploading" },
          { upload_id: "b", name: "y.md", size: 2, status: "error" },
          { upload_id: "c", name: "z.md", size: 3 },
        ]}
      />,
    );
    expect(screen.getByTestId("attachment-chip-uploading")).toBeInTheDocument();
    expect(screen.getByTestId("attachment-chip-error")).toBeInTheDocument();
    expect(screen.getByTestId("attachment-chip-done")).toBeInTheDocument();
  });

  it("图片附件直接渲染缩略图（带项目归属 URL）", () => {
    render(
      <AttachmentChips
        attachments={[{ upload_id: "img", name: "cat.png", size: 9, project_id: "p1" }]}
      />,
    );
    expect(screen.getByTestId("attachment-image-img")).toBeInTheDocument();
    expect(screen.getByAltText("cat.png")).toHaveAttribute(
      "src",
      expect.stringContaining("/api/company/uploads/img?project_id=p1"),
    );
  });

  it("parseEntryAttachments：JSON 字符串解析 + 破坏者容错", () => {
    const ok = parseEntryAttachments(
      JSON.stringify({ attachments: [{ upload_id: "u", name: "n", size: 4 }] }),
    );
    expect(ok).toEqual([{ upload_id: "u", name: "n", size: 4 }]);
    expect(parseEntryAttachments("{bad json")).toBeUndefined();
    expect(parseEntryAttachments(undefined)).toBeUndefined();
    expect(parseEntryAttachments(JSON.stringify({ attachments: "bad" }))).toBeUndefined();
  });

  it("MessageItem 用户消息渲染附件 chips", () => {
    render(
      <MessageItem
        message={{
          id: "m1",
          sessionId: "s1",
          role: "user",
          content: "hi",
          done: true,
          attachments: [{ upload_id: "u", name: "a.md", size: 5 }],
        }}
        interactive
      />,
    );
    expect(screen.getByTestId("attachment-chips")).toBeInTheDocument();
    expect(screen.getByText("a.md")).toBeInTheDocument();
  });
});

describe("⊕ 菜单「选择员工」可关（2026-09-30 单聊拍板；2026-10-02 收敛到项目群聊会话）", () => {
  it("allowTeamPicker=false：保留上传与@引用文件，仅摘掉「选择员工」", async () => {
    const user = userEvent.setup();
    render(
      <Composer
        busy={false}
        enhanced
        allowTeamPicker={false}
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    await user.click(screen.getByTestId("composer-plus"));
    expect(screen.getByTestId("composer-plus-attach")).toBeInTheDocument();
    expect(screen.getByTestId("composer-plus-quote")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-plus-team")).not.toBeInTheDocument();
  });

  it("回归护栏：不传该 prop 时「选择员工」照旧存在（项目群聊会话默认路径）", async () => {
    const user = userEvent.setup();
    render(<Composer busy={false} enhanced onSend={vi.fn()} onSteer={vi.fn()} onStop={vi.fn()} />);
    await user.click(screen.getByTestId("composer-plus"));
    expect(screen.getByTestId("composer-plus-team")).toBeInTheDocument();
  });
});

describe("⊕「选择员工」@团队成员（2026-10-02 用户拍板）", () => {
  it("点击 → 插入 @ 并打开 @ 面板；面板只列团队成员（mentionRoleIds 过滤，外部人不混入）", async () => {
    const user = userEvent.setup();
    render(
      <Composer
        busy={false}
        enhanced
        mentionRoleIds={["dev", "ops"]}
        onSend={vi.fn()}
        onSteer={vi.fn()}
        onStop={vi.fn()}
      />,
    );
    await user.click(screen.getByTestId("composer-plus"));
    await user.click(screen.getByTestId("composer-plus-team"));
    // 输入框插入 @（与 @引用对话文件同款交互）
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toContain("@");
    expect(await screen.findByTestId("composer-mentions")).toBeInTheDocument();
    expect(await screen.findByTestId("mention-item-employee-dev")).toBeInTheDocument();
    expect(screen.getByTestId("mention-item-employee-ops")).toBeInTheDocument();
    // 破坏者：项目外员工（全局在册）不得出现在团队成员列表
    expect(screen.queryByTestId("mention-item-employee-outsider")).not.toBeInTheDocument();
    // 点选员工 → 输入框出现 @该员工（@派单语义）
    await user.click(screen.getByTestId("mention-item-employee-dev"));
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toContain("@小美");
  });
});
