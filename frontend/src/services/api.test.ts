import { describe, expect, it } from "vitest";

import { formatValidationDetail } from "@/services/api";

describe("formatValidationDetail", () => {
  it("把 FastAPI 校验体 detail[{loc,msg}] 压成可读的 loc: msg", () => {
    const detail = [
      { loc: ["body", "employee_id"], msg: "Field required" },
      { loc: ["body", "role_id"], msg: "角色不存在" },
    ];
    expect(formatValidationDetail(detail)).toBe("employee_id: Field required; role_id: 角色不存在");
  });

  it("剥离 body/query 前缀段，只留字段路径", () => {
    expect(formatValidationDetail([{ loc: ["body", "nested", "name"], msg: "必填" }])).toBe(
      "nested.name: 必填",
    );
  });

  it("非数组 / 空数组返回 null（不得误报为校验错误）", () => {
    expect(formatValidationDetail(undefined)).toBeNull();
    expect(formatValidationDetail(null)).toBeNull();
    expect(formatValidationDetail([])).toBeNull();
    expect(formatValidationDetail("boom")).toBeNull();
  });

  it("loc 只剩 body 时退化为纯 msg", () => {
    expect(formatValidationDetail([{ loc: ["body"], msg: "整体非法" }])).toBe("整体非法");
  });
});
