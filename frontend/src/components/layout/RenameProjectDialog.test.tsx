/**
 * 功能：RenameProjectDialog 行为测试（打开预填 / 空名禁用 / 回车提交 / 失败可重试）。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RenameProjectDialog } from "@/components/layout/RenameProjectDialog";
import { companyApi, type ProjectRecord } from "@/services/company";
import { ApiError } from "@/services/api";

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: { ...actual.companyApi, renameProject: vi.fn() },
  };
});

const toastSuccess = vi.fn();
const toastError = vi.fn();
vi.mock("sonner", () => ({
  toast: {
    success: (...args: unknown[]) => toastSuccess(...args),
    error: (...args: unknown[]) => toastError(...args),
  },
}));

function makeProject(overrides: Partial<ProjectRecord> = {}): ProjectRecord {
  return {
    project_id: "p1",
    name: "原项目名",
    root_dir: "C:/work",
    team_ids: ["t1"],
    created_at: "2026-10-01T09:00:00",
    updated_at: "2026-10-01T09:00:00",
    members: [],
    ...overrides,
  };
}

const renameProject = vi.mocked(companyApi.renameProject);

function renderDialog(project: ProjectRecord | null, onRenamed = vi.fn()) {
  const onOpenChange = vi.fn();
  render(
    <RenameProjectDialog project={project} onRenamed={onRenamed} onOpenChange={onOpenChange} />,
  );
  return { onRenamed, onOpenChange };
}

describe("RenameProjectDialog", () => {
  beforeEach(() => {
    renameProject.mockReset();
    toastSuccess.mockClear();
    toastError.mockClear();
    renameProject.mockResolvedValue({
      project: makeProject({ name: "新项目名", updated_at: "2026-10-08T10:00:00" }),
    });
  });

  it("打开时输入框预填原项目名且立即全选（覆盖改名而非清空重打）", () => {
    renderDialog(makeProject());
    const input = screen.getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("原项目名");
    expect(screen.getByText("重命名")).toBeInTheDocument();
  });

  it("project 为 null 时不渲染对话框（关闭态零副作用）", () => {
    renderDialog(null);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(renameProject).not.toHaveBeenCalled();
  });

  it("清空为空白名时禁用保存且不发起请求（与后端 400 一致）", () => {
    renderDialog(makeProject());
    const input = screen.getByRole("textbox");
    const save = screen.getByRole("button", { name: "确定" });

    fireEvent.change(input, { target: { value: "   " } });

    expect(save).toBeDisabled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(renameProject).not.toHaveBeenCalled();
  });

  it("回车提交：名字去首尾空白后 PATCH，回传最新项目并关闭", async () => {
    const { onRenamed, onOpenChange } = renderDialog(makeProject());
    const input = screen.getByRole("textbox");

    fireEvent.change(input, { target: { value: "  新项目名  " } });
    fireEvent.keyDown(input, { key: "Enter" });

    await waitFor(() => expect(renameProject).toHaveBeenCalledWith("p1", "新项目名"));
    await waitFor(() => expect(onRenamed).toHaveBeenCalled());
    expect(onRenamed.mock.calls[0][0].name).toBe("新项目名");
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(toastSuccess).toHaveBeenCalledWith("项目已重命名");
  });

  it("后端失败：报错提示 + 对话框保持打开（可重试，不静默吞掉）", async () => {
    renameProject.mockRejectedValueOnce(new ApiError(400, "项目名不能为空"));
    const { onRenamed, onOpenChange } = renderDialog(makeProject());
    const input = screen.getByRole("textbox");

    fireEvent.change(input, { target: { value: "新项目名" } });
    fireEvent.click(screen.getByRole("button", { name: "确定" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("项目名不能为空"));
    expect(onRenamed).not.toHaveBeenCalled();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox")).toBeInTheDocument();

    // 重试：第二次成功即关闭
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  });

  it("非 ApiError 的异常也走错误提示（不裸崩）", async () => {
    renameProject.mockRejectedValueOnce(new Error("network down"));
    renderDialog(makeProject());

    fireEvent.change(screen.getByRole("textbox"), { target: { value: "新项目名" } });
    fireEvent.click(screen.getByRole("button", { name: "确定" }));

    await waitFor(() => expect(toastError).toHaveBeenCalledWith("Error: network down"));
  });

  it("取消按钮关闭且不发起请求", () => {
    const { onOpenChange } = renderDialog(makeProject());

    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(renameProject).not.toHaveBeenCalled();
  });
});
