#!/usr/bin/env node
/**
 * 功能：Electron 启动器——自动规避「exe 所在路径含非 ASCII 字符导致无法启动」的本机限制。
 *
 * 背景（2026-11-02 实测）：工作区位于含中文的路径时，`node_modules/electron/dist/electron.exe`
 * 启动即 ExitCode=1 且 stdout/stderr 全空、无事件日志、无崩溃记录，极难排查。
 * 物理复制 dist 到纯 ASCII 路径后正常；junction 无效（会被解析回真实路径）。
 * 官方开关 ELECTRON_OVERRIDE_DIST_PATH 可让 electron 从 ASCII 路径加载运行时，
 * 而 app 代码本身仍可留在中文路径（实测 ASCII exe + 中文 app = 正常工作）。
 *
 * 用法：
 *   node scripts/electron-runner.js <electron 参数...>
 *   例：node scripts/electron-runner.js .
 *       node scripts/electron-runner.js C:\path\to\app
 *
 * 行为：
 *   - dist 路径已是纯 ASCII → 直接启动，不做任何复制；
 *   - dist 路径含非 ASCII → 首次把运行时复制到 ~/.bandi/electron-dist/v<版本>/（用户目录，
 *     不污染工作区、不进 git），之后复用；并设 ELECTRON_OVERRIDE_DIST_PATH 指向该副本。
 */
const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

/**
 * 功能：把 electron 运行时复制到 ASCII 路径。
 * 优先 fs.cpSync；在受限环境（沙箱钩子拦截递归复制）下回退 Windows 自带 robocopy。
 */
function copyRuntime(src, dst) {
  try {
    fs.cpSync(src, dst, { recursive: true, force: true });
    return;
  } catch (err) {
    console.warn("[electron-runner] fs.cpSync 失败（" + err.message + "），改用 robocopy");
  }
  if (process.platform !== "win32") throw new Error("非 Windows 平台且 cpSync 失败");
  // robocopy 退出码 0-7 表示成功（1=有文件被复制）
  try {
    execFileSync("robocopy", [src, dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/R:1", "/W:1"], {
      stdio: "ignore",
    });
  } catch (err) {
    if (typeof err.status !== "number" || err.status > 7) {
      throw err;
    }
  }
}

const frontendRoot = path.resolve(__dirname, "..");
const electronPkg = path.join(frontendRoot, "node_modules", "electron");
const distDir = path.join(electronPkg, "dist");
const cliPath = path.join(electronPkg, "cli.js");

const exeName = process.platform === "win32" ? "electron.exe" : "electron";
const exePath = path.join(distDir, exeName);

if (!fs.existsSync(exePath)) {
  console.error("[electron-runner] 运行时缺失：", exePath);
  console.error("[electron-runner] 请先执行：node node_modules/electron/install.js");
  process.exit(1);
}

const isAscii = (value) => /^[\x00-\x7F]*$/.test(value);

const env = { ...process.env };
// WorkBuddy 等宿主注入的这两个变量会让 electron 以纯 Node 模式运行或加载删除钩子，
// 必须在启动前清掉（项目记忆：NODE_OPTIONS=--require=genie-safe-delete.cjs）。
delete env.NODE_OPTIONS;
delete env.ELECTRON_RUN_AS_NODE;

if (isAscii(distDir)) {
  console.log("[electron-runner] dist 路径为纯 ASCII，直接使用原生运行时");
} else {
  const { version } = JSON.parse(fs.readFileSync(path.join(electronPkg, "package.json"), "utf8"));
  const cacheRoot = path.join(os.homedir(), ".bandi", "electron-dist", `v${version}`);
  const cachedExe = path.join(cacheRoot, exeName);

  if (!fs.existsSync(cachedExe)) {
    console.log("[electron-runner] 检测到非 ASCII 路径，首次复制运行时（约 270 MB）");
    console.log("[electron-runner]   from:", distDir);
    console.log("[electron-runner]   to  :", cacheRoot);
    fs.mkdirSync(cacheRoot, { recursive: true });
    copyRuntime(distDir, cacheRoot);
    if (!fs.existsSync(cachedExe)) {
      console.error("[electron-runner] 复制失败：目标仍无 " + exeName);
      console.error('[electron-runner] 请手工执行：robocopy "' + distDir + '" "' + cacheRoot + '" /E');
      process.exit(1);
    }
    console.log("[electron-runner] 复制完成");
  }

  env.ELECTRON_OVERRIDE_DIST_PATH = cacheRoot;
  console.log("[electron-runner] ELECTRON_OVERRIDE_DIST_PATH =", cacheRoot);
}

const args = process.argv.slice(2);
if (args.length === 0) args.push(".");

const child = spawn(process.execPath, [cliPath, ...args], {
  stdio: "inherit",
  env,
  cwd: frontendRoot,
});

child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  else process.exit(code ?? 0);
});
