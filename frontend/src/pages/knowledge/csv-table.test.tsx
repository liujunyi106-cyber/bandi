// 功能：CSV 只读表格契约——BOM、筛选操作符、分组表头列、填色与视图态回写。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CsvTable } from "@/pages/knowledge/CsvTable";

const CSV = "\uFEFF姓名,城市,分数\n张三,上海,90\n李四,北京,70\n王五,上海,60\n";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CsvTable 视图态", () => {
  it("剥掉 BOM 并渲染表头与行", () => {
    render(<CsvTable initialState={{}} onPersist={() => undefined} text={CSV} />);
    expect(screen.getByRole("columnheader", { name: "姓名" })).toBeInTheDocument();
    expect(screen.getByText("张三")).toBeInTheDocument();
  });

  it("数值筛选（大于）只保留命中行", async () => {
    render(<CsvTable initialState={{}} onPersist={() => undefined} text={CSV} />);
    await userEvent.click(screen.getByRole("button", { name: "筛选" }));
    const selects = screen.getAllByRole("combobox");
    await userEvent.selectOptions(selects[0], "2");
    await userEvent.selectOptions(selects[1], "gt");
    await userEvent.type(screen.getByPlaceholderText("值"), "80");
    await userEvent.click(screen.getByRole("button", { name: "添加筛选" }));
    expect(screen.getByText("张三")).toBeInTheDocument();
    expect(screen.queryByText("李四")).not.toBeInTheDocument();
  });

  it("分组表头展示分组列的值与计数", async () => {
    render(<CsvTable initialState={{}} onPersist={() => undefined} text={CSV} />);
    await userEvent.click(screen.getByRole("button", { name: "分组" }));
    await userEvent.selectOptions(screen.getByRole("combobox"), "1");
    await userEvent.click(screen.getByRole("button", { name: "添加分组" }));
    expect(screen.getByText(/上海 · 2/)).toBeInTheDocument();
    expect(screen.getByText(/北京 · 1/)).toBeInTheDocument();
  });

  it("填色写进视图态（单元格背景 + onPersist）", async () => {
    const persist = vi.fn();
    render(<CsvTable initialState={{}} onPersist={persist} text={CSV} />);
    await userEvent.click(screen.getByText("张三"));
    await userEvent.click(screen.getByRole("button", { name: "填色" }));
    await userEvent.click(screen.getByRole("button", { name: "#fef3c7" }));
    await waitFor(() => expect(persist).toHaveBeenCalled());
    const state = persist.mock.calls.at(-1)?.[0] as { fills?: Record<string, string> };
    expect(Object.values(state.fills ?? {})).toContain("#fef3c7");
    expect(screen.getByText("张三").style.backgroundColor).not.toBe("");
  });
});
