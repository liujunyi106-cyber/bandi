/**
 * 真机验证：员工过程块四项修复（浏览器实跑）。
 * ① 时长不出现原始毫秒 ② 执行中即出过程块（非占位）③ 多员工各一条 ④ 记忆文案自解释。
 * 做法：真实 Chrome + 真实前端（vite 5173）+ 真实后端（8766，agnes-flash），
 * 通过页面上下文直接向 chat-store 注入过程槽（与 thread_progress 帧写入的是同一槽）。
 */
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright-core");
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 950 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto("http://127.0.0.1:5173/", { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  // 拿到真实 store（vite dev 下模块实例挂在 window 上不存在 → 走 React 内部不可行）
  // 改走接口路径：注入到真实 socket 帧处理器 ⇒ 与生产写入完全同源。
  const injected = await page.evaluate(async () => {
    // 必须用页面**实际加载的**模块 URL（HMR 会带 ?t= 时间戳；直接 import 无参版本
    // 会拿到另一个 store 实例，注入的数据 React 看不到）
    const entry = performance
      .getEntriesByType("resource")
      .map((e) => e.name)
      .find((n) => /\/src\/stores\/chat-store\.ts(\?|$)/.test(n));
    if (!entry) throw new Error("chat-store module URL not found in resource timeline");
    const mod = await import(entry);
    const store = mod.useChatStore.getState();
    // 必须用视图真实的 activeProjectId 写槽——过程块按项目作用域过滤
    const scope = store.activeProjectId || "company";
    const frame = (type, data) => ({ type, data });
    // 多员工并发：两名员工各一条线程过程槽（真实 thread_progress 帧的数据形态）
    store.applyEmployeeProgress(scope, "thread-A", "dev", frame("thinking_delta", { text: "正在检索竞品资料" }));
    store.applyEmployeeProgress(scope, "thread-A", "dev", frame("tool_call_start", { tool_name: "web_search", tool_input: { query: "竞品" } }));
    store.applyEmployeeProgress(scope, "thread-B", "qa", frame("thinking_delta", { text: "准备回归用例" }));
    store.applyEmployeeProgress(scope, "thread-B", "qa", frame("tool_call_start", { tool_name: "bash", tool_input: { command: "pytest" } }));
    return {
      moduleUrl: entry,
      scope,
      activeProjectId: store.activeProjectId ?? null,
      slots: Object.keys(mod.useChatStore.getState().employeeProgress),
      nodes: Object.values(mod.useChatStore.getState().employeeProgress).map((n) => n.length),
    };
  });
  await page.waitForTimeout(1200);

  // ② ③ 过程块实时出现（两个角色各一条）
  const debug = await page.evaluate(async () => {
    const mod = await import("/src/stores/chat-store.ts");
    const ui = await import("/src/stores/ui-store.ts");
    return {
      chatView: !!document.querySelector('[data-testid="chat-view"]'),
      url: location.href,
      slots: Object.keys(mod.useChatStore.getState().employeeProgress),
      projects: ui.useUiStore.getState().activeProjectId,
      keys: Object.keys(ui.useUiStore.getState()).filter((k) => /project/i.test(k)),
    };
  });
  console.log("  DEBUG " + JSON.stringify(debug));
  const liveCount = await page.locator('[data-testid="live-employee-progress-item"]').count();
  check("执行中即出过程块（非占位）", liveCount > 0, `live 块 ${liveCount} 条`);
  check(
    "多员工协作各一条",
    liveCount === 2,
    `期望 2（dev+qa），实际 ${liveCount}`,
  );

  const roleIds = await page
    .locator('[data-testid="live-employee-progress-item"]')
    .evaluateAll((els) => els.map((e) => e.getAttribute("data-role-id")));
  check("两个角色都在", roleIds.includes("dev") && roleIds.includes("qa"), roleIds.join(","));

  // ① 时长不出现原始毫秒
  const bodyText = await page.locator("body").innerText();
  const rawMs = bodyText.match(/\b\d{4,6}ms\b/g);
  check("无原始毫秒（如 42125ms）", !rawMs, rawMs ? `发现: ${rawMs.join(",")}` : "未发现");

  // 展开过程块看明细时长
  const toggles = page.locator('[data-testid="process-toggle"]');
  const toggleCount = await toggles.count();
  if (toggleCount > 0) {
    await toggles.first().click();
    await page.waitForTimeout(500);
    const detailText = await page.locator("body").innerText();
    const humanized = detailText.match(/\b\d+\.\d+s\b|\b\d+:\d{2}\b/g);
    check(
      "明细时长人类可读（4.2s / 1:05）",
      !/\b\d{4,6}ms\b/.test(detailText),
      humanized ? `样例: ${humanized.slice(0, 3).join(", ")}` : "（该轮无耗时明细）",
    );
  }

  // ④ 记忆文案
  const memOk = !bodyText.includes("记入记忆 x");
  check("旧文案「记入记忆 xN」已消失", memOk, memOk ? "未出现" : "仍出现");

  check("无 console error", errors.length === 0, errors.slice(0, 2).join(" | ") || "0 条");

  await page.screenshot({
    path: "C:\\Users\\10293\\Desktop\\深度学习与实践计划\\交付文档收集\\真机-员工过程块-2026-10-09.png",
    fullPage: false,
  });
  console.log(`\n注入槽: ${JSON.stringify(injected)}`);
  console.log(`结果: ${results.filter((r) => r.ok).length}/${results.length} PASS`);

  await browser.close();
  process.exit(results.every((r) => r.ok) ? 0 : 1);
})().catch((e) => {
  console.error("PROBE_ERROR", e);
  process.exit(2);
});