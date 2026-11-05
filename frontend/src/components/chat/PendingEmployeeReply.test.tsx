import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PendingEmployeeReply } from "@/components/chat/PendingEmployeeReply";

describe("PendingEmployeeReply", () => {
  it("发送后立即渲染员工头像、名字与回复状态", () => {
    render(<PendingEmployeeReply roleId="lead" name="Leader" />);
    const row = screen.getByTestId("pending-employee-reply");
    expect(row).toHaveAttribute("data-role-id", "lead");
    expect(row).toHaveTextContent("Leader");
    expect(screen.getByTestId("pending-employee-reply-status")).toHaveTextContent("正在回复中…");
    expect(row.className).not.toContain("pl-5");
  });
});
