/**
 * 真机：加载真实会话 sess-8edc88d4（DB 有完整 process），看过程块实际渲染。
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const SID = "sess-8edc88d4-0340-4a1f-a6ef-11e7d69e39dd";

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errs.push(m.text()));

  await page.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);

  // 通过 store 切到该会话（真实后端数据）
  const storeUrl = await page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .map((e) => e.name)
      .find((n) => /\/src\/stores\/chat-store\.ts(\?|$)/.test(n)),
  );
  await page.evaluate(async ({ storeUrl, sid }) => {
    const mod = await import(storeUrl);
    mod.useChatStore.getState().setActiveSession(sid);
  }, { storeUrl, sid: SID });
  await page.waitForTimeout(3500);

  const folded = await page.evaluate(() => {
    const toggles = Array.from(document.querySelectorAll('[data-testid="process-toggle"]'));
    return toggles.map((el) => ({
      text: el.innerText.replace(/\s+/g, " ").slice(0, 80),
      totalMs: el.querySelector('[data-testid="process-total-ms"]')?.textContent ?? null,
    }));
  });
  console.log("折叠态过程块:", JSON.stringify(folded, null, 1));

  // 展开最后一个过程块
  const toggles = page.locator('[data-testid="process-toggle"]');
  const n = await toggles.count();
  if (n > 0) {
    await toggles.last().click();
    await page.waitForTimeout(800);
  }
  const expanded = await page.evaluate(() => {
    const block = document.querySelectorAll('[data-testid="process-block"]');
    const last = block[block.length - 1];
    return {
      buckets: last
        ? Array.from(last.querySelectorAll('[data-testid^="process-bucket-"]')).map(
            (el) => el.getAttribute("data-testid"),
          )
        : [],
      notes: last
        ? Array.from(last.querySelectorAll('[data-testid="process-note"]')).map((el) =>
            el.innerText.trim(),
          )
        : [],
      text: last ? last.innerText.replace(/\s+/g, " ").slice(0, 260) : null,
    };
  });
  console.log("展开态:", JSON.stringify(expanded, null, 1));
  console.log("errors:", errs.length ? errs.slice(0, 3) : "0");

  await page.screenshot({
    path: "C:\\Users\\10293\\Desktop\\过程块真机-2026-10-09.png",
    fullPage: false,
  });
  await browser.close();
})().catch((e) => {
  console.error("PROBE_ERROR", e);
  process.exit(2);
});