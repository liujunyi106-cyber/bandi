import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AvatarEditor } from "@/components/team/AvatarEditor";
import { companyApi } from "@/services/company";

vi.mock("@/services/company", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/company")>();
  return {
    ...actual,
    companyApi: {
      ...actual.companyApi,
      upload: vi.fn(),
      uploadUrl: vi.fn((uploadId: string) => `/api/company/uploads/${uploadId}`),
    },
  };
});

const mocked = vi.mocked(companyApi);

function fileInput(): HTMLInputElement {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]');
  if (!input) throw new Error("file input not found");
  return input;
}

describe("AvatarEditor", () => {
  beforeEach(() => {
    vi.mocked(companyApi.upload).mockReset();
    vi.restoreAllMocks();
  });

  it("选择颜色回填 tone 并清空自定义头像", () => {
    const onChange = vi.fn();
    render(
      <AvatarEditor
        name="张三"
        tone={0}
        shape="classic"
        avatarUrl="/upload.png"
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByTestId("avatar-tone-9"));

    expect(onChange).toHaveBeenCalledWith({ tone: 9, shape: "classic", avatarUrl: "" });
  });

  it("选择样式回填 shape 并清空自定义头像", () => {
    const onChange = vi.fn();
    render(
      <AvatarEditor
        name="张三"
        tone={3}
        shape="classic"
        avatarUrl="/upload.png"
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByTestId("avatar-shape-capsule"));

    expect(onChange).toHaveBeenCalledWith({ tone: 3, shape: "capsule", avatarUrl: "" });
  });

  it("上传按钮以原生 label + input 呈现，且无自定义角标", () => {
    render(
      <AvatarEditor name="张三" tone={0} shape="dango" avatarUrl="" onChange={() => undefined} />,
    );

    expect(screen.getByRole("button", { name: /上传头像/ })).toBeInTheDocument();
    expect(screen.queryByText("自定义")).not.toBeInTheDocument();
  });

  it("选择合法图片：上传并回填 upload url", async () => {
    mocked.upload.mockResolvedValue({
      upload: {
        upload_id: "u1",
        name: "a.png",
        size: 3,
        content_type: "image/png",
        created_at: "",
      },
    } as never);
    const onChange = vi.fn();
    render(<AvatarEditor name="张三" tone={0} shape="classic" avatarUrl="" onChange={onChange} />);

    fireEvent.change(fileInput(), {
      target: { files: [new File(["png"], "a.png", { type: "image/png" })] },
    });

    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith({
        tone: 0,
        shape: "classic",
        avatarUrl: "/api/company/uploads/u1",
      }),
    );
  });

  it("取消选择：不调用上传、不报错", () => {
    const errorSpy = vi.spyOn(toast, "error");
    const onChange = vi.fn();
    render(<AvatarEditor name="张三" tone={0} shape="classic" avatarUrl="" onChange={onChange} />);

    fireEvent.change(fileInput(), { target: { files: [] } });

    expect(mocked.upload).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("非法格式：不上传并提示", () => {
    const errorSpy = vi.spyOn(toast, "error");
    const onChange = vi.fn();
    render(<AvatarEditor name="张三" tone={0} shape="classic" avatarUrl="" onChange={onChange} />);

    fireEvent.change(fileInput(), {
      target: { files: [new File(["gif"], "a.gif", { type: "image/gif" })] },
    });

    expect(mocked.upload).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
    expect(errorSpy).toHaveBeenCalledWith("仅支持 PNG / JPG / WebP 图片");
  });

  it("上传失败：提示错误，不写回头像", async () => {
    mocked.upload.mockRejectedValue(new Error("boom"));
    const errorSpy = vi.spyOn(toast, "error");
    const onChange = vi.fn();
    render(<AvatarEditor name="张三" tone={0} shape="classic" avatarUrl="" onChange={onChange} />);

    fireEvent.change(fileInput(), {
      target: { files: [new File(["png"], "a.png", { type: "image/png" })] },
    });

    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(onChange).not.toHaveBeenCalled();
  });
});
