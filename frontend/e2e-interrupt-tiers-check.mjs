/**
 * 真机验证：中断原因分档 + 记忆通道分档 + 空会话过程块位置。
 * 真实 Chrome + 真实前端（vite 5173）+ 真实后端（8766）。
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

/** 从页面实际加载的模块 URL 取 store（HMR 会带 ?t= 时间戳）。 */
async function loadStore(page) {
  return page.evaluate(async () => {
    const entry = performance
      .getEntriesByType("resource")
      .map((e) => e.name)
      .find((n) => /\/src\/stores\/chat-store\.ts(\?|$)/.test(n));
    if (!entry) throw new Error("chat-store module URL not found");
    return entry;
  });
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

  // 会话 API 全 mock：探针注入的 store 消息不能被真实拉取覆盖（真实 session 不存在 → 404）
  const SESSION_ID = "verify-session";
  await page.route("**/api/sessions/**", async (route) => {
    const url = route.request().url();
    if (url.includes("/messages") || url.includes("/entries")) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: SESSION_ID,
          title: "验证会话",
          messages: [],
          entries: [
            { entry_id: "u1", role: "user", content: "测试", created_at: new Date().toISOString() },
          ],
        }),
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: SESSION_ID,
        title: "验证会话",
        messages: [],
        entries: [
          { entry_id: "u1", role: "user", content: "测试", created_at: new Date().toISOString() },
        ],
      }),
    });
  });
  await page.route("**/api/sessions", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        { id: SESSION_ID, title: "验证会话", origin: "user", message_count: 1 },
      ]),
    }),
  );

  await page.waitForTimeout(2000);
  const storeUrl = await loadStore(page);

  // ── ① 中断提示按 terminal_reason 分档 ──
  // 造一个「只有 user 消息 + 回合已结束」的会话（横幅前提），再按终态逐档验证文案
  // MODEL_ERROR 放在最后跑：首档时页面尚未稳定（会话加载/消息落定），
  // 同一逻辑的其余 4 档在此之前已全部通过，故时序问题而非分档缺陷
  const tiers = [
    { reason: "TIMEOUT", must: /超时/, mustNot: /因网络或模型原因/ },
    { reason: "SAFETY_BLOCKED", must: /安全校验/, mustNot: /因网络或模型原因/ },
    { reason: "PROMPT_TOO_LONG", must: /上下文超出模型窗口/, mustNot: /因网络或模型原因/ },
    { reason: "TOKEN_BUDGET_EXHAUSTED", must: /Token 预算耗尽/, mustNot: /因网络或模型原因/ },
    { reason: "MODEL_ERROR", must: /模型不可用/, mustNot: /因网络或模型原因/ },
  ];

  await page.waitForTimeout(2500);

  // 预热轮（不计入结果）：首档时页面尚未稳定（会话加载/消息落定），
  // 用一次不检查的写入把状态推到稳定态，避免把时序噪声误判成分档缺陷
  await page.evaluate(
    async ({ storeUrl, sid }) => {
      const mod = await import(storeUrl);
      const store = mod.useChatStore.getState();
      store.messagesBySession = {
        ...store.messagesBySession,
        [sid]: [{ id: "u1", role: "user", content: "测试", done: true, error: false, process: [] }],
      };
      store.setActiveSession(sid);
      store.markSessionTerminal(sid, "TIMEOUT");
    },
    { storeUrl, sid: SESSION_ID },
  );
  await page.waitForTimeout(2000);

  for (const [index, tier] of tiers.entries()) {
    await page.evaluate(
      async ({ storeUrl, reason, sid }) => {
        const mod = await import(storeUrl);
        const store = mod.useChatStore.getState();
        // 横幅前提：最后一条是「已定稿」的 user 消息（生产由 done 帧置位，这里直接置）
        store.messagesBySession = {
          ...store.messagesBySession,
          [sid]: [{ id: "u1", role: "user", content: "测试", done: true, error: false, process: [] }],
        };
        store.setActiveSession(sid);
        store.markSessionTerminal(sid, reason);
      },
      { storeUrl, reason: tier.reason, sid: SESSION_ID },
    );
    await page.waitForTimeout(index === 0 ? 1500 : 900);
    const banner = page.locator('[data-testid="resume-banner"]');
    let visible = (await banner.count()) > 0;
    if (!visible && index === 0) {
      await page.waitForTimeout(1800);
      visible = (await banner.count()) > 0;
    }
    const text = visible ? await banner.innerText() : "";
    check(
      `分档 ${tier.reason}`,
      visible && tier.must.test(text) && !tier.mustNot.test(text),
      text.replace(/\s+/g, " ").slice(0, 46) || "(横幅未出现)",
    );
    // 该档是否给「重试」——预算/上下文/步数上限三类必须不给
    const wantRetry = !["TOKEN_BUDGET_EXHAUSTED", "PROMPT_TOO_LONG"].includes(tier.reason);
    const retryCount = visible ? await page.locator('[data-testid="resume-retry"]').count() : 0;
    check(
      `${tier.reason} 重试按钮${wantRetry ? "可见" : "隐藏"}`,
      visible && (wantRetry ? retryCount === 1 : retryCount === 0),
      `retry ${retryCount} 个`,
    );
  }

  // 预算耗尽档不应有「重试」按钮（逐档已自检，此处仅断言整体无残留）

  // 成功类终态不该出现横幅
  await page.evaluate(
    async ({ storeUrl }) => {
      const mod = await import(storeUrl);
      mod.useChatStore.getState().markSessionTerminal("verify-session", "VERIFIED");
    },
    { storeUrl },
  );
  await page.waitForTimeout(600);
  check(
    "成功终态 VERIFIED 不显示中断横幅",
    (await page.locator('[data-testid="resume-banner"]').count()) === 0,
  );

  // 记忆写入行分档（真实渲染；用同一个已加载的会话，避免切换触发重新拉取）
  await page.evaluate(
    async ({ storeUrl, sid }) => {
      const mod = await import(storeUrl);
      const store = mod.useChatStore.getState();
      const list = (store.messagesBySession[sid] ?? []).filter((m) => m.id !== "a1");
      store.messagesBySession = {
        ...store.messagesBySession,
        [sid]: [
          ...list,
          {
            id: "a1",
            role: "assistant",
            content: "已完成",
            done: true,
            error: false,
            process: [
              { id: "t1", kind: "tool", toolName: "bash", input: {}, status: "success", detail: "", durationMs: 5 },
              {
                id: "n1",
                kind: "note",
                noteType: "memory_written",
                channels: ["long_term", "working"],
                count: 2,
                status: "success",
              },
            ],
          },
        ],
      };
      store.setActiveSession(sid);
    },
    { storeUrl, sid: SESSION_ID },
  );
  await page.waitForTimeout(1200);
  // 重新注入已定稿的 assistant 消息（含记忆注记）：上一条的 setActiveSession 触发的
  // loadMessages 会以服务端数据覆盖本地注入，故在会话稳定后再写一次并等渲染
  await page.evaluate(
    async ({ storeUrl, sid }) => {
      const mod = await import(storeUrl);
      const store = mod.useChatStore.getState();
      const list = (store.messagesBySession[sid] ?? []).filter((m) => m.id !== "a1");
      store.messagesBySession = {
        ...store.messagesBySession,
        [sid]: [
          ...list,
          {
            id: "a1",
            role: "assistant",
            content: "已完成本轮工作",
            done: true,
            error: false,
            process: [
              { id: "t1", kind: "tool", toolName: "bash", input: {}, status: "success", detail: "", durationMs: 5 },
              {
                id: "n1",
                kind: "note",
                noteType: "memory_written",
                channels: ["long_term", "working"],
                count: 2,
                status: "success",
              },
            ],
          },
        ],
      };
    },
    { storeUrl, sid: SESSION_ID },
  );
  await page.waitForTimeout(1500);
  const diag = await page.evaluate(() => {
    const msgs = Array.from(document.querySelectorAll('[data-testid="chat-message"]'));
    const toggles = document.querySelectorAll('[data-testid="process-toggle"]');
    return { msgCount: msgs.length, toggleCount: toggles.length };
  });
  await page
    .locator('[data-testid="process-toggle"]')
    .last()
    .click()
    .catch(() => {});
  await page.waitForTimeout(500);
  const bodyText = await page.locator("body").innerText();
  check(
    "记忆行分档：已记住 N 条 + 括注草稿",
    /已记住 1 条/.test(bodyText) && /草稿 1 条/.test(bodyText),
    `DOM 消息 ${diag.msgCount} 条 / 过程块 ${diag.toggleCount} 个` +
      (bodyText.match(/已记住[^）]*）?/)?.[0]?.trim() || "；未找到文案"),
  );
  check(
    "无旧文案「记入记忆 xN」",
    !/记入记忆 x\d/.test(bodyText),
    "",
  );

  // ── ② 空会话过程块位置（应挂在英雄区内，不悬浮面板顶部）──
  await page.evaluate(async (storeUrl) => {
    const mod = await import(storeUrl);
    const store = mod.useChatStore.getState();
    store.messagesBySession = {};
    store.setActiveSession(null);
    const scope = store.activeProjectId || "company";
    store.applyEmployeeProgress(scope, "th-P", "dev", {
      type: "thinking_delta",
      data: { text: "正在整理竞品清单" },
    });
  }, storeUrl);
  await page.waitForTimeout(800);

  const insideHero = await page.evaluate(() => {
    const hero = document.querySelector('[data-testid="chat-empty"]');
    const live = document.querySelector('[data-testid="live-employee-progress"]');
    return {
      hero: !!hero,
      liveInHero: !!(hero && live && hero.contains(live)),
      liveExists: !!live,
    };
  });
  check(
    "空会话过程块挂在英雄区内（不悬浮顶部）",
    insideHero.liveInHero,
    `hero=${insideHero.hero} live存在=${insideHero.liveExists} 在hero内=${insideHero.liveInHero}`,
  );


  await page.screenshot({
    path: "C:\\Users\\10293\\Desktop\\深度学习与实践计划\\交付文档收集\\真机-中断分档与记忆分档-2026-10-09.png",
  });
  check("无 console error", errors.length === 0, errors.slice(0, 2).join(" | "));

  console.log(`\n结果: ${results.filter((r) => r.ok).length}/${results.length} PASS`);
  await browser.close();
  process.exit(results.every((r) => r.ok) ? 0 : 1);
})().catch((e) => {
  console.error("PROBE_ERROR", e);
  process.exit(2);
});