/**
  * 功能：InteractionCard 行为测试
 * -3a：权限三态 + notify 分流）。
 */
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InteractionCard } from "@/components/chat/InteractionCard";
import type { InteractionFrame } from "@/services/sessions";

const apiMocks = vi.hoisted(() => ({
  confirm: vi.fn<
    (
      sessionId: string,
      interactionId: string,
      decision: boolean,
      feedback?: string,
    ) => Promise<{ outcome: string }>
  >(async () => ({ outcome: "ok" })),
  clarify: vi.fn<
    (sessionId: string, interactionId: string, answer: string) => Promise<{ answered: boolean }>
  >(async () => ({ answered: true })),
  approvalDecision: vi.fn<
    (sessionId: string, decision: string, feedback?: string) => Promise<{ outcome: string }>
  >(async () => ({ outcome: "ok" })),
}));

vi.mock("@/services/sessions", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/sessions")>();
  return { ...actual, sessionsApi: apiMocks };
});

function frame(overrides: Partial<InteractionFrame> = {}): InteractionFrame {
  return {
    interaction_id: "i1",
    session_id: "s1",
    kind: "confirm",
    payload: { prompt: "允许执行 bash？" },
    ...overrides,
  };
}

describe("InteractionCard 倒计时", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("按 expires_in 显示 mm:ss 并逐秒递减", () => {
    vi.useFakeTimers();
    render(
      <InteractionCard frame={frame({ expires_in: 65 })} onResolved={vi.fn()} onError={vi.fn()} />,
    );
    expect(screen.getByTestId("interaction-countdown")).toHaveTextContent("1:05");
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(screen.getByTestId("interaction-countdown")).toHaveTextContent("1:03");
  });

  it("expires_in 缺省时不显示倒计时（旧后端兼容）", () => {
    render(<InteractionCard frame={frame()} onResolved={vi.fn()} onError={vi.fn()} />);
    expect(screen.queryByTestId("interaction-countdown")).not.toBeInTheDocument();
  });

  it("倒计时到点自动收起卡片（不再永久挂在输入框上方）", () => {
    vi.useFakeTimers();
    const onResolved = vi.fn();
    render(
      <InteractionCard
        frame={frame({ expires_in: 1 })}
        onResolved={onResolved}
        onError={vi.fn()}
      />,
    );
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    act(() => {
      vi.advanceTimersByTime(1300);
    });
    expect(onResolved).toHaveBeenCalledWith("i1");
  });

  it("破坏者：过期后显示「已超时」并禁用作答按钮", () => {
    render(
      <InteractionCard frame={frame({ expires_in: 0 })} onResolved={vi.fn()} onError={vi.fn()} />,
    );
    expect(screen.getByTestId("interaction-countdown")).toHaveTextContent("已超时");
    expect(screen.getByTestId("interaction-allow")).toBeDisabled();
    expect(screen.getByTestId("interaction-deny")).toBeDisabled();
  });

  it("choices 卡片超时后确认按钮禁用（作答不可提交）", () => {
    render(
      <InteractionCard
        frame={frame({
          kind: "input",
          payload: { prompt: "选择部署环境", choices: ["staging", "prod"] },
          expires_in: 0,
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    fireEvent.click(screen.getAllByTestId("interaction-choice")[0]);
    expect(screen.getByTestId("interaction-submit")).toBeDisabled();
  });
});

describe("InteractionCard 权限三态 + notify 分流（137b-3a）", () => {
  it("permission 卡：三态按钮（仅本次/本会话始终/拒绝）走 approval-decision 端点，不吃 choices 分支", async () => {
    const onResolved = vi.fn();
    render(
      <InteractionCard
        frame={frame({
          payload: {
            prompt: "Allow the agent to run `bash`?",
            ui_variant: "permission",
            permission_kind: "tool_run",
            detail: "写入 ~/work/out.csv",
            choices: ["Allow once", "Allow for session", "Deny"],
          },
        })}
        onResolved={onResolved}
        onError={vi.fn()}
      />,
    );
    expect(screen.getByTestId("permission-allow-once")).toBeInTheDocument();
    expect(screen.getByTestId("permission-allow-session")).toBeInTheDocument();
    expect(screen.getByTestId("permission-deny")).toBeInTheDocument();
    // 不走通用 choices 列表（防 clarify 文本通道误用）
    expect(screen.queryByTestId("interaction-choice")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interaction-submit")).not.toBeInTheDocument();
    // 明细行（detail）可读
    expect(screen.getByText("写入 ~/work/out.csv")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("permission-allow-session"));
    // 三态必须走 confirm+scope（生产真实机制）——
    // approval-decision 端点只认 ApprovalStore 的 pending，生产 API 模式走
    // ask_permission 挂起 ⇒ 该端点恒 409，点三态按钮必失败。
    await waitFor(() =>
      expect(apiMocks.confirm).toHaveBeenCalledWith("s1", "i1", true, undefined, "session"),
    );
    expect(apiMocks.approvalDecision).not.toHaveBeenCalled();
    expect(onResolved).toHaveBeenCalledWith("i1");
  });

  it("permission 卡「仅本次允许」→ confirm(scope=once)", async () => {
    apiMocks.confirm.mockClear();
    render(
      <InteractionCard
        frame={frame({ payload: { ui_variant: "permission", prompt: "允许？" } })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("permission-allow-once"));
    await waitFor(() =>
      expect(apiMocks.confirm).toHaveBeenCalledWith("s1", "i1", true, undefined, "once"),
    );
    expect(apiMocks.approvalDecision).not.toHaveBeenCalled();
  });

  it("B7：approval_store 来源的 permission 卡走 approval-decision（员工 headless 审批）", async () => {
    apiMocks.confirm.mockClear();
    apiMocks.approvalDecision.mockClear();
    render(
      <InteractionCard
        frame={frame({
          payload: {
            ui_variant: "permission",
            source: "approval_store",
            prompt: "请审批 bash",
            tool_call_id: "call-b7",
          },
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("permission-allow-once"));
    await waitFor(() =>
      expect(apiMocks.approvalDecision).toHaveBeenCalledWith(
        "s1",
        "allow_once",
        undefined,
        "call-b7",
      ),
    );
    expect(apiMocks.confirm).not.toHaveBeenCalled();
  });

  it("批量权限卡：一卡列出全部高危调用，一次决策全部生效", async () => {
    apiMocks.confirm.mockClear();
    const onResolved = vi.fn();
    render(
      <InteractionCard
        frame={frame({
          payload: {
            ui_variant: "permission",
            batch: true,
            tool_name: "bash",
            args: { command: "rm -rf build" },
            items: [
              {
                tool_call_id: "c1",
                tool_name: "bash",
                args: { command: "rm -rf build" },
                risk: "high",
              },
              { tool_call_id: "c2", tool_name: "fs_write", args: { path: "a.txt" }, risk: "high" },
            ],
          },
        })}
        onResolved={onResolved}
        onError={vi.fn()}
      />,
    );
    const list = screen.getByTestId("permission-batch-list");
    expect(list).toHaveTextContent("1. bash");
    expect(list).toHaveTextContent("2. fs_write");
    expect(screen.getByText("本次共 2 项高危操作，一次决策全部生效")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("permission-allow-session"));
    await waitFor(() =>
      expect(apiMocks.confirm).toHaveBeenCalledWith("s1", "i1", true, undefined, "session"),
    );
    expect(onResolved).toHaveBeenCalledWith("i1");
  });

  it("批量权限卡只列前 3 项，其余折叠为「还有 N 项」", () => {
    const items = Array.from({ length: 5 }, (_, index) => ({
      tool_call_id: `c${index}`,
      tool_name: `tool_${index + 1}`,
      args: { path: `f${index}.txt` },
      risk: "high",
    }));
    render(
      <InteractionCard
        frame={frame({ payload: { ui_variant: "permission", batch: true, items } })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    const list = screen.getByTestId("permission-batch-list");
    expect(list).toHaveTextContent("1. tool_1");
    expect(list).toHaveTextContent("3. tool_3");
    expect(list).not.toHaveTextContent("tool_4");
    expect(list).not.toHaveTextContent("tool_5");
    expect(screen.getByTestId("permission-batch-more")).toHaveTextContent("还有 2 项");
  });

  it("破坏者：notify 卡不渲染任何操作按钮 / 倒计时（非决策项——REV-137b1-10 分流）", () => {
    render(
      <InteractionCard
        frame={frame({
          payload: { notify: true, message: "已自动批准 bash（本会话始终允许）" },
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(screen.getByText("已自动批准 bash（本会话始终允许）")).toBeInTheDocument();
    expect(screen.queryByTestId("permission-deny")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interaction-deny")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interaction-allow")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interaction-submit")).not.toBeInTheDocument();
    expect(screen.queryByTestId("interaction-countdown")).not.toBeInTheDocument();
  });
});

describe("InteractionCard 多决策点分页 + 无描边", () => {
  it("questions 多决策点：<1/2> 可切换上下决策点，跨页收集答案并汇总 JSON 提交", async () => {
    apiMocks.clarify.mockClear();
    render(
      <InteractionCard
        frame={frame({
          payload: {
            questions: [
              { id: "q1", question: "选择部署环境", choices: ["staging", "prod"] },
              { id: "q2", question: "回滚策略？", choices: ["自动", "手动"] },
            ],
          },
          expires_in: 200,
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    // 第一页 + 页码
    expect(screen.getByTestId("decision-page")).toHaveTextContent("1/2");
    expect(screen.getByText("选择部署环境")).toBeInTheDocument();
    expect(screen.queryByText("回滚策略？")).not.toBeInTheDocument();
    // 切到第二页（犹豫也可来回切）
    fireEvent.click(screen.getByTestId("decision-next"));
    expect(screen.getByTestId("decision-page")).toHaveTextContent("2/2");
    expect(screen.getByText("回滚策略？")).toBeInTheDocument();
    fireEvent.click(screen.getByText("手动"));
    // 回第一页补齐
    fireEvent.click(screen.getByTestId("decision-prev"));
    fireEvent.click(screen.getByText("prod"));
    // 回末题再提交（多题模式下非末题主按钮=「下一题」，不提交）
    fireEvent.click(screen.getByTestId("decision-next"));
    fireEvent.click(screen.getByTestId("interaction-submit"));
    await waitFor(() => expect(apiMocks.clarify).toHaveBeenCalled());
    const answer = apiMocks.clarify.mock.calls[0][2] as string;
    expect(JSON.parse(answer)).toEqual({ q1: "prod", q2: "手动" });
  });

  it("卡片无描边 + 宽度略大于输入框", () => {
    render(<InteractionCard frame={frame()} onResolved={vi.fn()} onError={vi.fn()} />);
    const card = screen.getByTestId("interaction-card");
    expect(card.className).not.toContain("border-chat-hairline");
    expect(card.className).toContain("-mx-1");
  });

  it("生产形态（request_user_input 多问、题无选项）：每题文本框作答，答一题即可确定并汇总 JSON", async () => {
    apiMocks.clarify.mockClear();
    render(
      <InteractionCard
        frame={frame({
          payload: {
            questions: [
              { id: "q1", question: "部署到哪个环境？" },
              { id: "q2", question: "回滚策略？" },
            ],
          },
          expires_in: 200,
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    // 非末题主按钮为「下一题」（翻页语义）——不因未答禁用
    expect(screen.getByTestId("interaction-submit")).toHaveTextContent("下一题");
    fireEvent.change(screen.getByTestId("decision-input"), { target: { value: "prod" } });
    // 翻页后第二题未答：末题可确定（至少答一题；缺项由 Agent 追问）
    fireEvent.click(screen.getByTestId("decision-next"));
    expect(screen.getByTestId("interaction-submit")).toBeEnabled();
    expect(screen.getByTestId("interaction-submit")).toHaveTextContent("确定");
    fireEvent.change(screen.getByTestId("decision-input"), { target: { value: "手动" } });
    expect(screen.getByTestId("interaction-submit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("interaction-submit"));
    await waitFor(() => expect(apiMocks.clarify).toHaveBeenCalled());
    expect(JSON.parse(apiMocks.clarify.mock.calls[0][2] as string)).toEqual({
      q1: "prod",
      q2: "手动",
    });
  });

  it("多问分页：非末题主按钮=「下一题」（点击翻页不提交）；末题全未答时禁用（防误提交）", async () => {
    apiMocks.clarify.mockClear();
    render(
      <InteractionCard
        frame={frame({
          payload: {
            questions: [
              { id: "q1", question: "部署到哪个环境？" },
              { id: "q2", question: "回滚策略？" },
            ],
          },
          expires_in: 200,
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    // 第 1 页（非末题）：按钮为「下一题」——点击翻页且不提交
    expect(screen.getByTestId("interaction-submit")).toHaveTextContent("下一题");
    fireEvent.click(screen.getByTestId("interaction-submit"));
    expect(screen.getByTestId("decision-page")).toHaveTextContent("2/2");
    expect(apiMocks.clarify).not.toHaveBeenCalled();
    // 末题：全未答 → 禁用（防误提交）
    expect(screen.getByTestId("interaction-submit")).toHaveTextContent("确定");
    expect(screen.getByTestId("interaction-submit")).toBeDisabled();
    // 答一题即放开（部分作答语义保留）
    fireEvent.change(screen.getByTestId("decision-input"), { target: { value: "手动" } });
    expect(screen.getByTestId("interaction-submit")).toBeEnabled();
  });
});

describe("权限卡直接拒绝", () => {
  const permissionFrame = () =>
    frame({ payload: { ui_variant: "permission", prompt: "允许执行 bash？" } });

  it("permission 拒绝立即提交 deny 并收起卡片，不再要求理由", async () => {
    apiMocks.confirm.mockClear();
    const onResolved = vi.fn();
    render(<InteractionCard frame={permissionFrame()} onResolved={onResolved} onError={vi.fn()} />);
    fireEvent.click(screen.getByTestId("permission-deny"));
    expect(screen.queryByTestId("permission-deny-reason")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(apiMocks.confirm).toHaveBeenCalledWith("s1", "i1", false, undefined),
    );
    expect(onResolved).toHaveBeenCalledWith("i1");
  });

  it("普通确认卡拒绝同样直接提交 deny", async () => {
    apiMocks.confirm.mockClear();
    render(<InteractionCard frame={frame()} onResolved={vi.fn()} onError={vi.fn()} />);
    fireEvent.click(screen.getByTestId("interaction-deny"));
    expect(screen.queryByTestId("interaction-deny-reason")).not.toBeInTheDocument();
    await waitFor(() => expect(apiMocks.confirm).toHaveBeenCalledWith("s1", "i1", false));
  });
});

/**
 * 功能： 的三项卡片调整（截图反馈驱动）。
 *
 * 1. 卡序与题序合成**单一翻页器**（卡内、倒计时左侧）：› 前进一题（1/2 → 2/2）
 * 本卡最后一题再点 › 切到下一张卡；‹ 反向同理。不再并存两组 n/m；
 * 2. 去掉「确定（部分作答）」备注与左下角未答提示——确定按钮统一文案；
 * 3. 交互卡不再自铺底色（外层壳已同色，两层叠加会深半档 + 露出双圆角）。
 */
describe("InteractionCard 2026-10-01 调整", () => {
  const questionsFrame = (ids = ["q1", "q2"]) =>
    frame({
      kind: "input",
      payload: {
        questions: [
          { id: ids[0], question: "目标用户？", choices: ["C 端", "B 端"] },
          { id: ids[1], question: "预算？", choices: ["1 万", "5 万"] },
        ],
      },
      expires_in: 200,
    });

  const singleQuestionFrame = () =>
    frame({
      kind: "input",
      payload: { questions: [{ id: "q1", question: "目标用户？" }] },
      expires_in: 200,
    });

  it("单一翻页器：本卡内 › 前进一题 1/2 → 2/2，‹ 退回", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    expect(screen.getByTestId("decision-page")).toHaveTextContent("1/2");
    expect(screen.getByTestId("decision-prev")).toBeDisabled();
    fireEvent.click(screen.getByTestId("decision-next"));
    expect(screen.getByTestId("decision-page")).toHaveTextContent("2/2");
    fireEvent.click(screen.getByTestId("decision-prev"));
    expect(screen.getByTestId("decision-page")).toHaveTextContent("1/2");
  });

  it("本卡最后一题再点 › → 切到下一张卡（调 onNextCard，不再有第二组 n/m）", () => {
    const onNextCard = vi.fn();
    const onPrevCard = vi.fn();
    render(
      <InteractionCard
        frame={questionsFrame()}
        onResolved={vi.fn()}
        onError={vi.fn()}
        cardIndex={0}
        cardTotal={2}
        onPrevCard={onPrevCard}
        onNextCard={onNextCard}
      />,
    );
    // 卡序不在卡内单独显示（合成到单一翻页器），故不存在 card-page
    expect(screen.queryByTestId("card-page")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("decision-next")); // 1/2 → 2/2
    expect(onNextCard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("decision-next")); // 末题 → 下一张卡
    expect(onNextCard).toHaveBeenCalledTimes(1);
    expect(onPrevCard).not.toHaveBeenCalled();
  });

  it("本卡首题点 ‹ → 切到上一张卡（调 onPrevCard）", () => {
    const onPrevCard = vi.fn();
    render(
      <InteractionCard
        frame={questionsFrame()}
        onResolved={vi.fn()}
        onError={vi.fn()}
        cardIndex={1}
        cardTotal={2}
        onPrevCard={onPrevCard}
        onNextCard={vi.fn()}
      />,
    );
    expect(screen.getByTestId("decision-prev")).toBeEnabled();
    fireEvent.click(screen.getByTestId("decision-prev"));
    expect(onPrevCard).toHaveBeenCalledTimes(1);
  });

  it("边界：单卡单题无翻页器（无可翻内容）", () => {
    render(
      <InteractionCard frame={singleQuestionFrame()} onResolved={vi.fn()} onError={vi.fn()} />,
    );
    expect(screen.queryByTestId("decision-prev")).not.toBeInTheDocument();
    expect(screen.queryByTestId("decision-next")).not.toBeInTheDocument();
  });

  it("无题卡（权限卡）不渲染 ‹ ›：没有翻页数字就不该有箭头", () => {
    // 即使并存多张卡，权限卡自身也不显示箭头——无 n/m 即无翻页器
    render(
      <InteractionCard
        frame={frame({
          payload: { ui_variant: "permission", prompt: "允许执行 bash？" },
          expires_in: 300,
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
        cardIndex={0}
        cardTotal={2}
        onPrevCard={vi.fn()}
        onNextCard={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("decision-page")).not.toBeInTheDocument();
    expect(screen.queryByTestId("decision-prev")).not.toBeInTheDocument();
    expect(screen.queryByTestId("decision-next")).not.toBeInTheDocument();
    // 倒计时仍在（无翻页器 ≠ 无头部信息）
    expect(screen.getByTestId("interaction-countdown")).toBeInTheDocument();
  });

  it("有题卡 + 多卡：箭头保留（承担跨卡切换），数字为本题序", () => {
    render(
      <InteractionCard
        frame={questionsFrame()}
        onResolved={vi.fn()}
        onError={vi.fn()}
        cardIndex={0}
        cardTotal={2}
        onPrevCard={vi.fn()}
        onNextCard={vi.fn()}
      />,
    );
    expect(screen.getByTestId("decision-page")).toHaveTextContent("1/2");
    expect(screen.getByTestId("decision-prev")).toBeInTheDocument();
    expect(screen.getByTestId("decision-next")).toBeInTheDocument();
  });

  it("边界：末题答任一题后「确定」可用（部分作答语义保留，仅去掉文案备注）", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    // 非末题主按钮=「下一题」（翻页语义，不受未答影响——防误提交）
    expect(screen.getByTestId("interaction-submit")).toHaveTextContent("下一题");
    expect(screen.getByTestId("interaction-submit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("decision-next")); // 到第 2 题（末题，有选项）
    fireEvent.click(screen.getByRole("button", { name: "1 万" })); // 仅答一题
    expect(screen.getByTestId("interaction-submit")).toBeEnabled();
    expect(screen.getByTestId("interaction-submit")).toHaveTextContent("确定");
    expect(screen.getByTestId("interaction-submit").textContent).not.toContain("部分作答");
  });

  it("确定按钮不再渲染未答提示（左下角说明已移除）", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    expect(screen.queryByText(/可先跳过/)).not.toBeInTheDocument();
    expect(screen.queryByText(/未作答时确定/)).not.toBeInTheDocument();
  });

  it("交互卡不自铺底色（外壳提供底色，避免两层同色叠加成双圆角）", () => {
    render(<InteractionCard frame={frame()} onResolved={vi.fn()} onError={vi.fn()} />);
    const cls = screen.getByTestId("interaction-card").className;
    expect(cls).not.toContain("bg-primary/5");
  });

  it("有选项时**并存**自由输入框", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    // 第 1 题带两个选项 + 一个输入框
    expect(screen.getAllByTestId("interaction-choice")).toHaveLength(2);
    expect(screen.getByTestId("decision-input")).toBeInTheDocument();
  });

  it("选项与输入框共用同一答案键：选中选项后输入框置空，手打则覆盖选项", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    const input = screen.getByTestId("decision-input") as HTMLInputElement;
    const dot = () => screen.getByRole("button", { name: "C 端" }).querySelector("span");
    const rowClass = () => screen.getByRole("button", { name: "C 端" }).className;

    fireEvent.click(screen.getByRole("button", { name: "C 端" }));
    expect(input.value).toBe(""); // 选中的是选项，不在输入框重复显示
    // 选中**只变左侧圆圈**，行本身恒为白底（不换底/不换字色）
    expect(dot()?.className).toContain("bg-primary");
    expect(rowClass()).toContain("bg-card");

    fireEvent.change(input, { target: { value: "其他：D 端" } });
    expect(input.value).toBe("其他：D 端");
    // 手打覆盖选项 ⇒ 圆圈回到未选中
    expect(dot()?.className).not.toContain("bg-primary");
    expect(rowClass()).toContain("bg-card");
  });

  it("聚焦自由输入框自动取消已选选项（点输入即视为要手打）", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    const input = screen.getByTestId("decision-input") as HTMLInputElement;
    const dot = () => screen.getByRole("button", { name: "C 端" }).querySelector("span");

    fireEvent.click(screen.getByRole("button", { name: "C 端" }));
    expect(dot()?.className).toContain("bg-primary");
    fireEvent.focus(input);
    expect(dot()?.className).not.toContain("bg-primary"); // 点击输入 → 选项自动取消
  });

  it("选项条恒白底无描边 + 输入框无描边无焦点效果", () => {
    render(<InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />);
    const row = screen.getByRole("button", { name: "C 端" });
    expect(row.className, "选项条必须白底").toContain("bg-card");
    expect(row.className, "选项条不得有描边").not.toContain("border");
    expect(row.className, "未选中不得是半透明白").not.toContain("bg-card/60");

    const input = screen.getByTestId("decision-input");
    expect(input.className, "答题输入框不得有描边").not.toContain("border");
    expect(input.className, "答题输入框不得有焦点变色").not.toContain("focus:");
  });

  it("allow_custom=false 且带选项时才只留按钮（不渲染输入框）", () => {
    render(
      <InteractionCard
        frame={frame({
          kind: "input",
          payload: {
            allow_custom: false,
            questions: [
              { id: "q1", question: "目标？", choices: ["A", "B"] },
              { id: "q2", question: "预算？", choices: ["1 万", "5 万"] },
            ],
          },
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(screen.getAllByTestId("interaction-choice")).toHaveLength(2);
    expect(screen.queryByTestId("decision-input")).not.toBeInTheDocument();
  });

  it("图标区分：权限卡 ShieldQuestion、提问卡 ListChecks（风格一致：同 lucide 同尺寸）", () => {
    const { container, unmount } = render(
      <InteractionCard
        frame={frame({ payload: { ui_variant: "permission", prompt: "允许？" } })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    expect(container.querySelector(".lucide-shield-question")).toBeTruthy();
    unmount();
    const { container: c2 } = render(
      <InteractionCard frame={questionsFrame()} onResolved={vi.fn()} onError={vi.fn()} />,
    );
    expect(c2.querySelector(".lucide-list-checks")).toBeTruthy();
    expect(c2.querySelector(".lucide-shield-question")).toBeNull();
  });

  it("提问卡取消：提交空答案（Agent 按信息缺失继续），不提交部分作答 JSON", async () => {
    apiMocks.clarify.mockClear();
    render(
      <InteractionCard
        frame={frame({
          kind: "input",
          payload: { questions: [{ id: "q1", question: "目标？" }] },
        })}
        onResolved={vi.fn()}
        onError={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("decision-input"), { target: { value: "先写一半" } });
    fireEvent.click(screen.getByTestId("interaction-cancel"));

    await waitFor(() => expect(apiMocks.clarify).toHaveBeenCalledTimes(1));
    expect(apiMocks.clarify.mock.calls[0][2]).toBe("");
  });
});
