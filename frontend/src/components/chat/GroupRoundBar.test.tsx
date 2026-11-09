import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GroupRoundBar } from "@/components/chat/GroupRoundBar";
import { companyApi, type GroupRoundSnapshot } from "@/services/company";
import { useChatStore } from "@/stores/chat-store";

function round(status: string): GroupRoundSnapshot {
  return {
    round_id: "r1",
    session_id: "s1",
    project_id: "p1",
    status,
    relay_count: 1,
    relay_limit: 6,
    slots: [
      {
        slot_id: "slot-1",
        role_id: "lead",
        role_name: "负责人",
        status: "speaking",
        origin: "user",
        called_by: "",
        order: 0,
        work_item_id: "wi-1",
      },
      {
        slot_id: "slot-2",
        role_id: "worker",
        role_name: "执行",
        status: "waiting",
        origin: "user",
        called_by: "",
        order: 1,
        work_item_id: "",
      },
    ],
  };
}

describe("GroupRoundBar", () => {
  beforeEach(() => {
    useChatStore.setState({ roundsBySession: {} });
    vi.restoreAllMocks();
  });

  it("renders speaking and waiting slots", () => {
    render(<GroupRoundBar round={round("running")} />);
    expect(screen.getByTestId("group-round-bar")).toBeInTheDocument();
    expect(screen.getByTestId("group-round-slot-lead")).toHaveTextContent("正在回复");
    expect(screen.getByTestId("group-round-slot-worker")).toHaveTextContent("等待");
  });

  it("resumes a paused round and updates the store", async () => {
    const paused = round("paused");
    const resumed = { ...paused, status: "running" };
    vi.spyOn(companyApi, "resumeRound").mockResolvedValue({
      round: resumed,
      work_item_id: "wi-2",
    });
    render(<GroupRoundBar round={paused} />);
    await userEvent.click(screen.getByTestId("group-round-resume"));
    await waitFor(() => {
      expect(companyApi.resumeRound).toHaveBeenCalledWith("r1");
      expect(useChatStore.getState().roundsBySession.s1?.status).toBe("running");
    });
  });
});
