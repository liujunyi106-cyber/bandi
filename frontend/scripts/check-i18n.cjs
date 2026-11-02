#!/usr/bin/env node
/**
 * 功能：校验 i18n 文案 key 漂移（zh 为基准，en 必须完全对齐）。
 * 用法：npm run i18n:check —— 不一致时打印差集并以退出码 1 失败。
  * @base docs/前端设计规划.md （`i18n:check` 防 key 漂移，StaffDeck 对齐）
 */
const fs = require("node:fs");
const path = require("node:path");

const I18N_DIR = path.join(__dirname, "..", "src", "i18n");

function readJson(file) {
  const abs = path.join(I18N_DIR, file);
  if (!fs.existsSync(abs)) {
    console.error(`✗ 缺失文案文件：${abs}`);
    process.exit(1);
  }
  return JSON.parse(fs.readFileSync(abs, "utf8"));
}

function flatten(node, prefix = "", out = new Set()) {
  for (const [key, value] of Object.entries(node)) {
    const full = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      flatten(value, full, out);
    } else {
      out.add(full);
    }
  }
  return out;
}

const zhKeys = flatten(readJson("zh.json"));
const enKeys = flatten(readJson("en.json"));

const SRC_DIR = path.join(__dirname, "..", "src");

function collectSourceFiles(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const abs = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "i18n" || entry.name === "node_modules") continue;
      collectSourceFiles(abs, out);
    } else if (/\.(ts|tsx)$/.test(entry.name)) {
      out.push(abs);
    }
  }
  return out;
}

/** 功能：收集代码里字面量引用的 key（t("x") / translate(locale, "x")），模板串动态 key 不计。 */
function collectUsedKeys() {
  const used = new Map();
  const patterns = [
    /\bt\(\s*["']([\w.@/-]+)["']/g,
    /\btranslate\(\s*[^,]+,\s*["']([\w.@/-]+)["']/g,
  ];
  for (const file of collectSourceFiles(SRC_DIR)) {
    const text = fs.readFileSync(file, "utf8");
    for (const pattern of patterns) {
      for (const match of text.matchAll(pattern)) {
        if (!used.has(match[1])) used.set(match[1], path.relative(SRC_DIR, file));
      }
    }
  }
  return used;
}

const usedKeys = collectUsedKeys();
const missingInEn = [...zhKeys].filter((key) => !enKeys.has(key)).sort();
const extraInEn = [...enKeys].filter((key) => !zhKeys.has(key)).sort();
const usedMissing = [...usedKeys]
  .filter(([key]) => !zhKeys.has(key))
  .sort((a, b) => a[0].localeCompare(b[0]));

if (missingInEn.length === 0 && extraInEn.length === 0 && usedMissing.length === 0) {
  console.log(`✓ i18n keys aligned (${zhKeys.size} keys, ${usedKeys.size} referenced)`);
  process.exit(0);
}

console.error("✗ i18n 校验失败：");
if (missingInEn.length) console.error(`  en 缺少 ${missingInEn.length} 项：\n    ${missingInEn.join("\n    ")}`);
if (extraInEn.length) console.error(`  en 多出 ${extraInEn.length} 项：\n    ${extraInEn.join("\n    ")}`);
if (usedMissing.length) {
  console.error(
    `  代码引用但 zh 缺失 ${usedMissing.length} 项：\n    ${usedMissing
      .map(([key, file]) => `${key}  (${file})`)
      .join("\n    ")}`,
  );
}
process.exit(1);
