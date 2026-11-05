/**
 * 功能：左栏交互改动的真机验证（2026-10-09 用户拍板两项）。
 *
 * 覆盖：
 *  1. 项目上下文下点通用「新建会话」→ 必须回到未选中项目的普通会话新建态（URL 无 ?project=）
 *  2. 项目行「更多」按钮：默认不可见不可点（opacity 0 / pointer-events none），悬浮后可见可点
 *  3. 截图留证（项目上下文 / 新建会话后）
 *
 * 用法（frontend/ 下）：node <此文件>
 * 依赖：dev server 5173 + 后端 8766 均在跑；复用系统 Chrome（与 shot_ui.mjs 同法）。
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const BASE = "http://127.0.0.1:5173";
const SHOT_DIR = process.env.SHOT_DIR || ".";
const results = [];

function check(label, ok, detail = "") {
  results.push({ label, ok });
  console.log(`  [${ok ? "OK " : "FAIL"}] ${label}${detail ? `  ${detail}` : ""}`);
}

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (error) => errors.push(String(error)));

// 进项目上下文（直接用已存在的评测项目）
await page.goto(`${BASE}/?project=eval`, { waitUntil: "networkidle" });
await page.waitForTimeout(1500);
await page.screenshot({ path: `${SHOT_DIR}/1-项目上下文.png` });

const projectRows = page.locator('[data-testid="project-menu-trigger"]');
const rowCount = await projectRows.count();
check("项目行已渲染（拿到 more 触发器）", rowCount > 0, `count=${rowCount}`);

const activitySlots = await page.locator('[data-testid^="project-activity-slot-"]').count();
check("项目行渲染状态槽（内容数据驱动）", activitySlots > 0, `slots=${activitySlots}`);

if (rowCount > 0) {
  const trigger = projectRows.first();
  const before = await trigger.evaluate((el) => {
    const style = getComputedStyle(el);
    return { opacity: style.opacity, pointerEvents: style.pointerEvents };
  });
  check(
    "项目「更多」默认不可见不可点",
    before.opacity === "0" && before.pointerEvents === "none",
    JSON.stringify(before),
  );

  await trigger.locator("xpath=..").hover();
  await page.waitForTimeout(300);
  const after = await trigger.evaluate((el) => {
    const style = getComputedStyle(el);
    return { opacity: style.opacity, pointerEvents: style.pointerEvents };
  });
  check(
    "悬浮项目行后「更多」可见可点",
    after.opacity === "1" && after.pointerEvents === "auto",
    JSON.stringify(after),
  );
  await page.screenshot({ path: `${SHOT_DIR}/2-项目行悬浮.png` });

  // 移开鼠标 → 恢复隐形
  await page.mouse.move(700, 500);
  await page.waitForTimeout(300);
  const away = await trigger.evaluate((el) => getComputedStyle(el).opacity);
  check("鼠标移开后「更多」复原为隐形", away === "0", `opacity=${away}`);

  // 1. 项目上下文下点「新建会话」→ 回未选中态
  const newSession = page.locator('[data-testid="sidebar-new-session"]');
  if ((await newSession.count()) > 0) {
    await newSession.first().click();
    await page.waitForTimeout(800);
    const url = page.url();
    check("点「新建会话」后 URL 不再带项目", !url.includes("project="), url);
    await page.screenshot({ path: `${SHOT_DIR}/3-新建会话后.png` });
  } else {
    check("找到左栏「新建会话」按钮", false, "sidebar-new-session 未找到");
  }
}

check("无页面 JS 错误", errors.length === 0, errors.slice(0, 2).join(" | "));

await browser.close();
const failed = results.filter((item) => !item.ok);
console.log(`\n结论：${results.length - failed.length}/${results.length} 通过`);
process.exit(failed.length ? 1 : 0);
