/**
 * 第 7 项真机：审批预览改提炼内容——打开通知中心截图。
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));

  await page.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  // 打开通知中心
  await page.locator('[data-testid="notification-trigger"]').click();
  await page.waitForTimeout(1200);

  const state = await page.evaluate(() => {
    const list = document.querySelector('[data-testid="approval-memory-list"]');
    return {
      listExists: !!list,
      text: list ? list.innerText.replace(/\s+/g, " ").slice(0, 300) : document.body.innerText.replace(/\s+/g, " ").slice(0, 400),
    };
  });
  console.log("审批列表:", JSON.stringify(state, null, 1));
  const ok = state.listExists && /事实 · /.test(state.text) && !/我是 \*\*Agnes\*\*/.test(state.text);
  console.log(ok ? "PASS 预览为提炼内容（寒暄已跳过）" : "CHECK 输出");

  await page.screenshot({ path: "C:\\Users\\10293\\Desktop\\记忆审批预览-2026-10-10.png", fullPage: false });
  console.log("截图已保存到桌面");
  console.log("errors:", errs.length ? errs.slice(0, 2) : "0");
  await browser.close();
})().catch((e) => {
  console.error("PROBE_ERROR", e);
  process.exit(2);
});