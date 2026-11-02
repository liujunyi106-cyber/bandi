/**
 * 功能：通知中心内「团队记忆待确认」交互测试（Phase2-2； 形态）。
 * 钉行为不钉实现：行渲染 → 面板内内联展开 → 批准/驳回 → 原地已处理态 → 列表刷新。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationMenu } from "@/components/layout/NotificationMenu";
import { memoryApprovalApi, skillApprovalApi } from "@/services/approvals";
import { teamMemoryApi } from "@/services/team-memory";
import { useChatStore } from "@/stores/chat-store";

vi.mock("@/services/team-memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/team-memory")>();
  return {
    ...actual,
    teamMemoryApi: {
      list: vi.fn(),
      confirm: vi.fn(),
      reject: vi.fn(),
      report: vi.fn(),
    },
  };
});

vi.mock("@/services/approvals", () => ({
  memoryApprovalApi: { list: vi.fn(), approve: vi.fn(), reject: vi.fn() },
  skillApprovalApi: { list: vi.fn(), approve: vi.fn(), reject: vi.fn() },
}));

const mockedApprovals = {
  memory: vi.mocked(memoryApprovalApi),
  skill: vi.mocked(skillApprovalApi),
};

/** 功能：构造一条待批准记忆条目（前端消费口径的最小形态）。 */
function memoryRow(id: string, summary = `记忆 ${id}`) {
  return { id, subsystem: "memory", action: "store_long_term", summary, created_at: 1 };
}

const mocked = vi.mocked(teamMemoryApi);

const record = {
  id: "a1b2c3d4",
  summary:
    "[反思] 任务：优化慢查询\n判定：pass（功能完成无风险）\n分析：逐条核对完成\n产出：根因是缺复合索引",
  payload: { team_id: "team-a", project_id: "proj-e2e", employee_id: "emp-1" },
  created_at: 1790572426,
};

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  render(
    <MemoryRouter>
      <NotificationMenu />
    </MemoryRouter>,
  );
  await user.click(screen.getByTestId("notification-trigger"));
  return user;
}

describe("NotificationMenu · 团队记忆待确认", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ notifications: [] });
    mockedApprovals.memory.list.mockResolvedValue({ pending: [] });
    mockedApprovals.skill.list.mockResolvedValue({ pending: [] });
  });

  it("并入通知中心：待确认项渲染为一行（摘要首行 + 员工×团队 + 时间）", async () => {
    mocked.list.mockResolvedValue({ pending: [record] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const list = await screen.findByTestId("team-memory-list");
    expect(list).toBeTruthy();
    // 行标题用原因，不用整段任务原文
    expect(screen.getAllByText("功能完成无风险").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/emp-1 · team-a/).length).toBeGreaterThan(0);
    // 默认选中第一条，右侧详情直接可见
    expect(screen.getByTestId("team-memory-card")).toBeTruthy();
    expect(screen.getByTestId("team-memory-approve")).toBeTruthy();
  });

  it("有审批项时不得同时显示「暂无通知」（L1 真机报障：下面两条、底部空态）", async () => {
    // 破坏者视角：空态曾只按 notifications 判空，记忆审批有 2 条时底部仍写
    // 「暂无通知」——列表与空态自相矛盾。
    useChatStore.setState({ notifications: [] });
    mocked.list.mockResolvedValue({ pending: [] });
    mockedApprovals.memory.list.mockResolvedValue({
      pending: [memoryRow("m-1"), memoryRow("m-2")],
    });
    mockedApprovals.skill.list.mockResolvedValue({ pending: [] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await waitFor(() => expect(screen.getAllByText(/记忆 m-1/).length).toBeGreaterThan(0));
    expect(screen.queryByText("暂无通知")).toBeNull();
  });

  it("四类内容全空时才显示空态", async () => {
    useChatStore.setState({ notifications: [] });
    mocked.list.mockResolvedValue({ pending: [] });
    mockedApprovals.memory.list.mockResolvedValue({ pending: [] });
    mockedApprovals.skill.list.mockResolvedValue({ pending: [] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    expect(await screen.findByText("暂无通知")).toBeTruthy();
  });

  it("红点数字 = 待确认项数（无未读通知时）", async () => {
    mocked.list.mockResolvedValue({ pending: [record] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(
      <MemoryRouter>
        <NotificationMenu />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("1")).toBeTruthy());
    await user.click(screen.getByTestId("notification-trigger"));
    expect(await screen.findByTestId("team-memory-list")).toBeTruthy();
  });

  it("点击该行在面板内原地内联展开审批卡（非弹窗）", async () => {
    mocked.list.mockResolvedValue({ pending: [record] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await user.click(await screen.findByTestId("team-memory-toggle"));
    const card = await screen.findByTestId("team-memory-card");
    expect(card).toBeTruthy();
    // 全文（判定 + 分析 + 产出）+ 来源行 + 两动作
    expect(card.textContent).toContain("根因是缺复合索引");
    expect(card.textContent).toContain("逐条核对完成");
    expect(card.textContent).toContain("emp-1 · proj-e2e · team-a");
    expect(screen.getByTestId("team-memory-approve")).toBeTruthy();
    expect(screen.getByTestId("team-memory-reject")).toBeTruthy();
    // 仍是同一个菜单（无 dialog/新窗口）
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("上级会签意见标注「仅建议」", async () => {
    mocked.list.mockResolvedValue({
      pending: [
        {
          ...record,
          payload: { ...record.payload, escalation: { opinion: "同意沉淀", reporter_role: "cto" } },
        },
      ],
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await user.click(await screen.findByTestId("team-memory-toggle"));
    const advice = await screen.findByTestId("team-memory-advice");
    expect(advice.textContent).toContain("仅建议");
    expect(advice.textContent).toContain("cto");
    expect(advice.textContent).toContain("同意沉淀");
  });

  it("批准 → 落库端点 + 原地已处理态 + 列表刷新", async () => {
    mocked.list.mockResolvedValue({ pending: [record] });
    mocked.confirm.mockResolvedValue({ confirmed: true, id: record.id });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await user.click(await screen.findByTestId("team-memory-toggle"));
    await user.click(await screen.findByTestId("team-memory-approve"));

    await waitFor(() =>
      expect(mocked.confirm).toHaveBeenCalledWith(record.id, {
        create_skill: true,
        skill_name: "团队经验",
      }),
    );
    expect(mocked.reject).not.toHaveBeenCalled();
    // 原地变已处理态
    expect(await screen.findByTestId("team-memory-resolved")).toHaveProperty(
      "textContent",
      "已批准进团队记忆",
    );
    // 列表自动刷新（已处理项不再可点）
    await waitFor(() => expect(screen.queryByTestId("team-memory-toggle")).toBeNull());
    expect(mocked.list.mock.calls.length).toBeGreaterThan(1);
  });

  it("驳回 → 丢弃端点（不落库）", async () => {
    mocked.list.mockResolvedValue({ pending: [record] });
    mocked.reject.mockResolvedValue({ rejected: true, id: record.id });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await user.click(await screen.findByTestId("team-memory-toggle"));
    await user.click(await screen.findByTestId("team-memory-reject"));

    await waitFor(() => expect(mocked.reject).toHaveBeenCalledWith(record.id));
    expect(mocked.confirm).not.toHaveBeenCalled();
    expect(await screen.findByTestId("team-memory-resolved")).toHaveProperty(
      "textContent",
      "已驳回",
    );
  });

  it("无待确认项时不出现该分区（面板仍是纯通知列表）", async () => {
    mocked.list.mockResolvedValue({ pending: [] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    expect(await screen.findByText("暂无通知")).toBeTruthy();
    expect(screen.queryByTestId("team-memory-list")).toBeNull();
  });

  it("接口失败可读（错误可见，不静默）", async () => {
    mocked.list.mockRejectedValue(new Error("boom-500"));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", "boom-500");
  });
});

/* 待批准分区与「清空通知」的动作语义——61 条批量复现后收敛的行为契约。 */
describe("NotificationMenu · 待批准分区与清空动作", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useChatStore.setState({ notifications: [] });
    mockedApprovals.skill.list.mockResolvedValue({ pending: [] });
    mocked.list.mockResolvedValue({ pending: [] });
  });

  it("待批准条目渲染为行（点批准调批准端点后列表刷新）", async () => {
    // 挂载与打开各拉一次；批准后队列应真实减少 ⇒ 用可变快照模拟服务端出队
    let rows = [memoryRow("aa11bb22", "记忆一")];
    mockedApprovals.memory.list.mockImplementation(async () => ({ pending: rows }));
    mockedApprovals.memory.approve.mockImplementation(async () => {
      rows = [];
      return { approved: true, id: "aa11bb22" };
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const row = await screen.findByTestId("approval-row-personal");
    expect(row.getAttribute("data-approval-id")).toBe("aa11bb22");
    await user.click(screen.getByTestId("approval-approve-personal"));

    await waitFor(() => expect(mockedApprovals.memory.approve).toHaveBeenCalledWith("aa11bb22"));
    await waitFor(() => expect(screen.queryByTestId("approval-row-personal")).toBeNull());
    expect(mockedApprovals.memory.reject).not.toHaveBeenCalled();
  });

  it("点不采纳调驳回端点（不落库）", async () => {
    mockedApprovals.memory.list.mockResolvedValue({ pending: [memoryRow("cc33dd44")] });
    mockedApprovals.memory.reject.mockResolvedValue({ rejected: true });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await user.click(await screen.findByTestId("approval-reject-personal"));
    await waitFor(() => expect(mockedApprovals.memory.reject).toHaveBeenCalledWith("cc33dd44"));
    expect(mockedApprovals.memory.approve).not.toHaveBeenCalled();
  });

  it("批准失败必达（全局 toast，不只藏在面板顶部）", async () => {
    mockedApprovals.memory.list.mockResolvedValue({ pending: [memoryRow("ee55ff66")] });
    mockedApprovals.memory.approve.mockRejectedValue(new Error("replay-failed"));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await user.click(await screen.findByTestId("approval-approve-personal"));
    expect(await screen.findByText("replay-failed")).toBeTruthy();
  });

  it("批量积压时只展示前 5 条，可展开全部（面板不被单类内容淹没）", async () => {
    const many = Array.from({ length: 12 }, (_, index) => memoryRow(`aa0000${index.toString(16)}`));
    mockedApprovals.memory.list.mockResolvedValue({ pending: many });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    await waitFor(() => expect(screen.getAllByTestId("approval-row-personal")).toHaveLength(5));
    expect(screen.getByTestId("approval-show-all")).toBeTruthy();
    await user.click(screen.getByTestId("approval-show-all"));
    await waitFor(() => expect(screen.getAllByTestId("approval-row-personal")).toHaveLength(12));
  });

  it("清空钮清理整条待审批队列（驳回不落库；2026-10-13 用户拍板合并为面板清空）", async () => {
    let rows = Array.from({ length: 3 }, (_, index) => memoryRow(`bb0000${index.toString(16)}`));
    mockedApprovals.memory.list.mockImplementation(async () => ({ pending: rows }));
    mockedApprovals.memory.reject.mockImplementation(async () => {
      rows = [];
      return { rejected: true };
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const clear = await screen.findByTestId("clear-notifications");
    await waitFor(() => expect(clear).toBeEnabled());
    await user.click(clear);
    await waitFor(() => expect(mockedApprovals.memory.reject).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.queryByTestId("approval-memory-list")).toBeNull());
    expect(mockedApprovals.memory.approve).not.toHaveBeenCalled();
  });

  it("面板全空时清空钮禁用（无可清内容不做假动作）", async () => {
    useChatStore.setState({ notifications: [] });
    mockedApprovals.memory.list.mockResolvedValue({ pending: [] });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const clear = await screen.findByTestId("clear-notifications");
    expect(clear).toBeDisabled();
  });

  it("有通知时清空钮清通知；审批清空后全空才转禁用", async () => {
    useChatStore.setState({
      notifications: [{ id: "n1", kind: "info", title: "通知一", ts: 1, read: false }],
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const clear = await screen.findByTestId("clear-notifications");
    expect(clear).toBeEnabled();
    await user.click(clear);
    await waitFor(() => expect(useChatStore.getState().notifications).toHaveLength(0));
    expect(await screen.findByTestId("clear-notifications")).toBeDisabled();
  });

  it("清空钮常驻筛选栏右侧，并与页头关闭钮对齐", async () => {
    useChatStore.setState({
      notifications: [{ id: "n1", kind: "info", title: "通知一", ts: 1, read: false }],
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const clear = await screen.findByTestId("clear-notifications");
    expect(clear.className).toContain("text-muted-foreground");
    const filterRow = clear.closest('[data-testid="notification-filter-row"]');
    expect(filterRow).not.toBeNull();
    expect(clear.closest("header")).toBeNull();
    expect(screen.getByTestId("notification-close").closest("header")).not.toBeNull();
  });

  it("筛选选中项为黑色胶囊，无可见边框", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    await openMenu(user);

    const all = screen.getByRole("button", { name: "全部" });
    expect(all.className).toContain("bg-foreground");
    expect(all.className).toContain("text-background");
    expect(all.className).toContain("border-transparent");
  });
});

