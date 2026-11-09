import { describe, expect, it } from "vitest";

import { stripGeneratedCitationBlock } from "@/lib/citation-cleanup";

describe("stripGeneratedCitationBlock", () => {
  it("删除尾部引用来源清单", () => {
    const content =
      "产品保修期为两年。\n\n引用来源：\n[1] warranty.md（知识库条目「保修政策」，chunk_id: kchunk:abc）";
    expect(stripGeneratedCitationBlock(content)).toBe("产品保修期为两年。");
  });

  it("保留正文中的来源表述", () => {
    const content = "这个结论来自多个来源：访谈、日志和工单，需要进一步核对。";
    expect(stripGeneratedCitationBlock(content)).toBe(content);
  });

  it("无来源清单时原样返回", () => {
    const content = "第一段。\n\n第二段。";
    expect(stripGeneratedCitationBlock(content)).toBe(content);
  });

  it("保留单行泛化来源", () => {
    const content = "结论。\n\n来源：https://example.com/a";
    expect(stripGeneratedCitationBlock(content)).toBe(content);
  });

  it("保留泛化来源的单个文件说明", () => {
    const content = "结论。\n\n来源：详见 docs/config.json";
    expect(stripGeneratedCitationBlock(content)).toBe(content);
  });

  it("删除多项列表来源", () => {
    const content = "结论。\n\n来源：\n- https://example.com/a\n- https://example.com/b";
    expect(stripGeneratedCitationBlock(content)).toBe("结论。");
  });

  it("整条回复只有来源清单时保留原文", () => {
    const content = "引用来源：[1] warranty.md（chunk_id: kchunk:abc）";
    expect(stripGeneratedCitationBlock(content)).toBe(content);
  });

  it("代码块内的来源标题不删除", () => {
    const content = "示例：\n\n```text\n引用来源：[1] warranty.md\n```";
    expect(stripGeneratedCitationBlock(content)).toBe(content);
  });
});
