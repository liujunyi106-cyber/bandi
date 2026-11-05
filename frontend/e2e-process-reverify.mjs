/**
 * 真机验证（数据清空后）：真实前端发消息 → 过程块出现 → 时长 m+s 格式 → 无选区效果。
 * 全程真实：真实内核 8766 + 真实 agnes 模型 + 真实会话落库。
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? " -- " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errs.push(m.text()));

  await page.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  check("清空后为空态（无残留会话）", (await page.locator('[data-testid="chat-empty"]').count()) > 0);

  // 在真实 Composer 里输入并发送
  const input = page.locator("textarea").first();
  await input.click();
  await input.fill("你好呀，你能帮我做什么？");
  await page.waitForTimeout(400);
  await input.press("Enter");
  console.log("  已发送，等待回合完成（agnes，最多 280s）…");

  // 等待回复完成：过程块出现且总行含「已完成」或回复正文出现
  let done = false;
  for (let i = 0; i < 56; i++) {
    await page.waitForTimeout(5000);
    const state = await page.evaluate(() => {
      const toggles = Array.from(document.querySelectorAll('[data-testid="process-toggle"]'));
      const body = document.body.innerText;
      return {
        toggles: toggles.map((el) => el.innerText.replace(/\s+/g, " ").slice(0, 60)),
        hasReply: /AI 助手|秘书|我能|可以帮/.test(body.slice(0, 4000)),
      };
    });
    if (state.toggles.some((t) => t.includes("已完成"))) {
      done = true;
      console.log(`  回合完成（${(i + 1) * 5}s 内）：`, JSON.stringify(state.toggles));
      break;
    }
  }
  check("回合完成且过程块出现", done);

  // 展开过程块，验证时长格式与内容
  const toggles = page.locator('[data-testid="process-toggle"]');
  if (await toggles.count()) {
    await toggles.last().click();
    await page.waitForTimeout(700);
  }
  const detail = await page.evaluate(() => {
    const blocks = document.querySelectorAll('[data-testid="process-block"]');
    const last = blocks[blocks.length - 1];
    if (!last) return null;
    return {
      total: last.querySelector('[data-testid="process-total-ms"]')?.textContent ?? null,
      text: last.innerText.replace(/\s+/g, " ").slice(0, 160),
      buckets: Array.from(last.querySelectorAll('[data-testid^="process-bucket-count-"]')).map(
        (el) => el.textContent,
      ),
      selectNone: last.className.includes("select-none"),
    };
  });
  console.log("  过程块明细:", JSON.stringify(detail));

  const total = detail?.total ?? "";
  // 时长格式：<60s → 4.2s / 42.1s；≥60s → 3m11s。禁止 M:SS（含冒号）
  const badColon = /^\d+:\d{2}$/.test(total);
  check("时长非 M:SS 形态（无冒号歧义）", !badColon, `total=${JSON.stringify(total)}`);
  check(
    "时长格式合法（Nms / N.Ns / NmNs）",
    /^(\d+ms|\d+\.\d+s|\d+m\d+s)$/.test(total),
    `total=${JSON.stringify(total)}`,
  );
  check("过程块禁用文本选中（select-none）", Boolean(detail?.selectNone));
  check("思考桶存在（子过程可见）", (detail?.buckets ?? []).some((b) => /思考|web|工具|知识/.test(b ?? "")), JSON.stringify(detail?.buckets));

  await page.screenshot({ path: "C:\\Users\\10293\\Desktop\\过程块真机重验-2026-10-09.png", fullPage: false });
  check("无 console error", errs.length === 0, errs.slice(0, 2).join(" | "));

  console.log(`\n结果: ${results.filter((r) => r.ok).length}/${results.length} PASS`);
  await browser.close();
  process.exit(results.every((r) => r.ok) ? 0 : 1);
})().catch((e) => {
  console.error("PROBE_ERROR", e);
  process.exit(2);
});