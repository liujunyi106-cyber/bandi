// 功能：Markdown 块解析/序列化契约——图片、表格、媒体链接与常见块往返。
import { describe, expect, it } from "vitest";

import { newBlock, parseMarkdown, serializeBlocks } from "./markdown-blocks";

describe("markdown-blocks", () => {
  it("parses image and link into media blocks", () => {
    const blocks = parseMarkdown("![图](http://a/1.png)\n\n[附件](http://a/f.pdf)\n");
    const image = blocks.find((block) => block.type === "image");
    const file = blocks.find((block) => block.type === "file");
    expect(image?.text).toBe("图|http://a/1.png");
    expect(file?.text).toBe("附件|http://a/f.pdf");
  });

  it("serializes image, video and file blocks", () => {
    const blocks = [
      newBlock("image", "封面|http://a/c.png"),
      newBlock("video", "录屏|http://a/v.mp4"),
      newBlock("file", "说明|http://a/f.pdf"),
    ];
    const markdown = serializeBlocks(blocks);
    expect(markdown).toContain("![封面](http://a/c.png)");
    expect(markdown).toContain("[录屏](http://a/v.mp4)");
    expect(markdown).toContain("[说明](http://a/f.pdf)");
  });

  it("parses a pipe table and keeps it as a table block", () => {
    const markdown = "| 城市 | 金额 |\n| --- | --- |\n| 上海 | 100 |\n";
    const blocks = parseMarkdown(markdown);
    const table = blocks.find((block) => block.type === "table");
    expect(table).toBeDefined();
    expect(table?.text).toContain("| 上海 | 100 |");
    expect(serializeBlocks(blocks)).toContain("| 上海 | 100 |");
  });

  it("roundtrips heading and paragraph", () => {
    const blocks = parseMarkdown("# 标题\n\n正文内容\n");
    expect(blocks.map((block) => block.type)).toEqual(["h1", "text"]);
    expect(serializeBlocks(blocks).trim()).toBe("# 标题\n\n正文内容");
  });

  it("roundtrips alignment container and centered text", () => {
    const blocks = parseMarkdown(":::center\n居中文字\n:::\n");
    expect(blocks[0].align).toBe("center");
    expect(blocks[0].text).toBe("居中文字");
    expect(serializeBlocks(blocks)).toContain(":::center\n居中文字\n:::");
  });

  it("keeps code fence language on roundtrip", () => {
    const blocks = parseMarkdown("```python\nprint(1)\n```\n");
    expect(blocks[0].type).toBe("code");
    expect(blocks[0].language).toBe("python");
    expect(serializeBlocks(blocks)).toBe("```python\nprint(1)\n```\n");
  });

  it("roundtrips soft line break inside one paragraph", () => {
    const block = newBlock("text", "第一行\n第二行");
    const markdown = serializeBlocks([block]);
    expect(markdown).toContain("第一行<br>第二行");
    const parsed = parseMarkdown(markdown);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].text).toBe("第一行\n第二行");
  });
});
