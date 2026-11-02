#!/usr/bin/env node
/**
 * 功能：sidecar 链路冒烟（Day147 C-2/C-3）——不需要 Electron/GUI。
 *
 * 模拟壳视角完整链路：
 *   spawn `python -m bandi serve --port 0`（BANDI_HOME 指到隔离目录）
 *   → stdout 读 `BANDI_SIDECAR_READY port=N` 行
 *   → GET /ready 期望 200
 *   → taskkill /PID /T /F 树杀（与 electron/sidecar.ts 同口径）
 *   → 断言：退出后 127.0.0.1:N 不再应答（端口真正释放，无孤儿进程）。
 *
 * 用法：node scripts/sidecar-smoke.cjs [python 解释器路径]
 *   未传解释器时：优先 bandi/.venv/Scripts/python.exe，回退 PATH 里的 python，
 *   并自动把 .venv/Lib/site-packages 拼进 PYTHONPATH（沙箱口径）。
 */
const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const BANDI_DIR = path.join(REPO_ROOT, "bandi");
const VENV_SITE = path.join(BANDI_DIR, ".venv", "Lib", "site-packages");
const READY_PREFIX = "BANDI_SIDECAR_READY";

function resolvePython(explicit) {
  if (explicit) return explicit;
  const venvPython = path.join(BANDI_DIR, ".venv", "Scripts", "python.exe");
  if (fs.existsSync(venvPython)) return venvPython;
  return "python";
}

async function main() {
  const python = resolvePython(process.argv[2]);
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "bandi-sidecar-smoke-"));
  const env = {
    ...process.env,
    BANDI_HOME: home,
    PYTHONPATH: [BANDI_DIR, VENV_SITE, process.env.PYTHONPATH || ""].join(path.delimiter),
    BANDI_SERVE_SPA: "0",
    BANDI_SCHEDULER_AUTOSTART: "0",
  };
  console.log(`[smoke] python = ${python}`);
  console.log(`[smoke] BANDI_HOME = ${home}`);

  const child = spawn(python, ["-m", "bandi", "serve", "--host", "127.0.0.1", "--port", "0"], {
    cwd: BANDI_DIR,
    env,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.setEncoding("utf-8");
  child.stderr.setEncoding("utf-8");
  child.stderr.on("data", (c) => process.stderr.write(`[sidecar] ${c}`));

  const port = await new Promise((resolve, reject) => {
    const deadline = setTimeout(() => reject(new Error("60s 内未读到就绪行")), 60_000);
    let tail = "";
    child.stdout.on("data", (chunk) => {
      tail = (tail + chunk).slice(-2000);
      for (const line of tail.split("\n")) {
        const match = new RegExp(`^${READY_PREFIX} port=(\\d+) pid=(\\d+)$`).exec(line.trim());
        if (match) {
          clearTimeout(deadline);
          resolve({ port: Number(match[1]), pid: Number(match[2]) });
          return;
        }
      }
    });
    child.on("exit", (code) => {
      clearTimeout(deadline);
      reject(new Error(`sidecar 提前退出 exitCode=${code}`));
    });
  });
  console.log(`[smoke] ✅ stdout 就绪行：port=${port.port} pid=${port.pid}`);

  // 端口文件（与 electron/sidecar.ts 通道②同口径）
  const portFile = JSON.parse(fs.readFileSync(path.join(home, "sidecar.port"), "utf-8"));
  if (portFile.port !== port.port || portFile.pid !== port.pid) {
    throw new Error(`端口文件与 stdout 不一致：${JSON.stringify(portFile)}`);
  }
  console.log(`[smoke] ✅ 端口文件一致`);

  const deadline = Date.now() + 30_000;
  let ok = false;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port.port}/ready`, {
        signal: AbortSignal.timeout(2000),
      });
      if (response.status === 200) {
        ok = true;
        break;
      }
    } catch {
      /* 服务还在起，继续轮询 */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  if (!ok) throw new Error("/ready 未在 30s 内返回 200");
  console.log(`[smoke] ✅ /ready = 200`);

  // 树杀（与 SidecarManager.stop() 同口径）
  execFileSync("taskkill", ["/PID", String(port.pid), "/T", "/F"], { stdio: "ignore" });
  await new Promise((r) => setTimeout(r, 1000));

  // 断言：端口不再应答（无孤儿进程占着端口继续服务）
  let stillAlive = false;
  try {
    await fetch(`http://127.0.0.1:${port.port}/ready`, { signal: AbortSignal.timeout(2000) });
    stillAlive = true;
  } catch {
    /* 预期：连接失败 */
  }
  if (stillAlive) throw new Error("树杀后端口仍应答——存在孤儿进程");
  console.log(`[smoke] ✅ 树杀后端口已释放，无孤儿进程`);
  console.log(`\n[smoke] ALL PASS — sidecar 链路（spawn→端口→ready→树杀）完整可用`);
  process.exit(0);
}

main().catch((error) => {
  console.error(`\n[smoke] FAIL：${error.message}`);
  process.exit(1);
});
