import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AutomationPage from "@/pages/automation";
import { schedulerApi, type ScheduledTask } from "@/services/scheduler";
import { settingsApi } from "@/services/settings";

vi.mock("@/services/scheduler", () => ({
  schedulerApi: {
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
    trigger: vi.fn(),
    status: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
  },
}));

vi.mock("@/services/settings", () => ({
  settingsApi: { mcpServers: vi.fn() },
}));

const TASK: ScheduledTask = {
  task_id: "st-1",
  name: "每日 AI 新闻",
  cron: "30 12 * * *",
  prompt: "拉取过去 24 小时的 AI 新闻",
  enabled: true,
  status: "idle",
  next_run_at: "2026-10-31T12:30:00+08:00",
  last_run_at: "2026-10-30T12:30:00+08:00",
  last_error: null,
  last_output: "5 条摘要",
  target_type: "assistant",
  target_id: "",
  description: "",
  session_id: "sess-1",
  project_id: "",
  employee_id: "",
  schedule_kind: "cron",
  schedule_spec: { cron: "30 12 * * *" },
  effective_from: "",
  effective_until: "",
  mcp_servers: [],
};

describe("AutomationPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(schedulerApi.list).mockResolvedValue({ tasks: [TASK] });
    vi.mocked(schedulerApi.start).mockResolvedValue({ running: true });
    vi.mocked(settingsApi.mcpServers).mockResolvedValue({
      servers: [
        {
          name: "github",
          pool: "github",
          transport: "stdio",
          command: "npx",
          args: [],
          url: "",
          prefix: "github",
          enabled: true,
          category: "",
          homepage: "",
          allowed_tools: null,
          status: "connected",
          tool_count: 1,
          tools: ["github_issue"],
        },
      ],
      global: { autostart: true, lazy_exposure: false, max_exposed_tools: 20 },
    });
  });

  it("renders task list from scheduler API", async () => {
    render(
      <MemoryRouter>
        <AutomationPage />
      </MemoryRouter>,
    );
    expect(await screen.findByText("每日 AI 新闻")).toBeInTheDocument();
    expect(screen.getByText("30 12 * * *")).toBeInTheDocument();
    expect(screen.getByText("助理")).toBeInTheDocument();
  });

  it("opens detail and shows run information", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AutomationPage />
      </MemoryRouter>,
    );
    await user.click(await screen.findByText("每日 AI 新闻"));
    await waitFor(() => expect(screen.getByText("运行信息")).toBeInTheDocument());
    expect(screen.getByDisplayValue("30 12 * * *")).toBeInTheDocument();
    expect(screen.getByText("5 条摘要")).toBeInTheDocument();
  });

  it("creates a periodic task with a structured schedule", async () => {
    vi.mocked(schedulerApi.create).mockResolvedValue(TASK);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AutomationPage />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "新建任务" }));
    await user.type(screen.getByPlaceholderText("例如：每日 AI 新闻"), "每天九点");
    await user.type(screen.getByPlaceholderText("到点要执行什么"), "生成日报");
    await user.click(screen.getByRole("button", { name: /^保存$/ }));
    await waitFor(() => expect(schedulerApi.create).toHaveBeenCalled());
    expect(schedulerApi.create).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "每天九点",
        schedule_kind: "periodic",
        schedule_spec: expect.objectContaining({ unit: "daily", hour: 9, minute: 0 }),
      }),
    );
  });

  it("stores selected MCP tools on the task", async () => {
    vi.mocked(schedulerApi.create).mockResolvedValue(TASK);
    const user = userEvent.setup();
    render(
      <MemoryRouter>
        <AutomationPage />
      </MemoryRouter>,
    );
    await user.click(await screen.findByRole("button", { name: "新建任务" }));
    await user.type(screen.getByPlaceholderText("例如：每日 AI 新闻"), "带连接器");
    await user.type(screen.getByPlaceholderText("到点要执行什么"), "查 issue");
    await user.click(await screen.findByRole("button", { name: "issue" }));
    await user.click(screen.getByRole("button", { name: /^保存$/ }));
    await waitFor(() => expect(schedulerApi.create).toHaveBeenCalled());
    expect(schedulerApi.create).toHaveBeenCalledWith(
      expect.objectContaining({ mcp_servers: ["github_issue"] }),
    );
  });
});
