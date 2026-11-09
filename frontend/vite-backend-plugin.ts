/**
 * 功能：dev 环境下 vite dev server 启动时自动拉起内核（uvicorn），退出时回收。
 * 端口已有健康内核则复用；端口被不健康进程占用时显式报错，不静默复用。
 * apply: "serve"（仅开发），不影响 build。
 */
import { spawn, type ChildProcess } from "node:child_process";
import { request } from "node:http";
import { createConnection } from "node:net";
import { fileURLToPath } from "node:url";

import type { Plugin } from "vite";

function probe(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const sock = createConnection({ host, port });
    sock.once("connect", () => {
      sock.destroy();
      resolve(true);
    });
    sock.once("error", () => resolve(false));
  });
}

function healthProbe(host: string, port: number, timeoutMs = 1500): Promise<boolean> {
  return new Promise((resolve) => {
    const req = request(
      { host, port, path: "/health", method: "GET", timeout: timeoutMs },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.once("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.once("error", () => resolve(false));
    req.end();
  });
}

export function backendLauncher(kernelOrigin: string): Plugin {
  const { hostname: host, port: portStr } = new URL(kernelOrigin);
  const port = Number(portStr || 80);
  let proc: ChildProcess | null = null;

  const kill = () => {
    if (proc?.pid) {
      spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { stdio: "ignore" });
      proc = null;
    }
  };

  return {
    name: "bandi-backend-launcher",
    apply: "serve",
    async configureServer(server) {
      // vitest 跑测试时不启动/接管内核
      if (process.env.VITEST) return;
      if (await probe(host, port)) {
        if (await healthProbe(host, port)) {
          console.log(`[bandi] kernel healthy on ${host}:${port} - reuse`);
          return;
        }
        console.error(
          `[bandi] ${host}:${port} is occupied but /health is not responding. ` +
            "Kill the stale kernel process, then restart the dev server.",
        );
        return;
      }
      const bandiDir = fileURLToPath(new URL("../bandi", import.meta.url));
      const python = fileURLToPath(new URL("../bandi/.venv/Scripts/python.exe", import.meta.url));
      // 不使用 --reload：Windows 下 reload 子进程是 SelectorEventLoop，
      // create_subprocess_exec 不可用，bash/python_exec/git 会全部失效。
      proc = spawn(
        python,
        ["-m", "uvicorn", "bandi.api.app:app", "--host", host, "--port", String(port)],
        {
          cwd: bandiDir,
          stdio: "inherit",
          env: {
            ...process.env,
            PYTHONUTF8: "1",
            PYTHONUNBUFFERED: "1",
            BANDI_STARTUP_TRACE: "1",
          },
        },
      );
      console.log(`[bandi] kernel starting (pid=${proc.pid}) - ${kernelOrigin}`);
      const child = proc;
      child.once("exit", (code) => console.log(`[bandi] kernel exited (code=${code ?? "signal"})`));
      const startedAt = Date.now();
      void (async () => {
        for (let i = 0; i < 120; i += 1) {
          await new Promise((resolve) => setTimeout(resolve, 500));
          if (child.exitCode !== null) return;
          if (await healthProbe(host, port, 1000)) {
            console.log(`[bandi] kernel ready in ${((Date.now() - startedAt) / 1000).toFixed(1)}s`);
            return;
          }
        }
        console.error("[bandi] kernel did not become healthy within 60s; check API logs.");
      })();
      server.httpServer?.once("close", kill);
      process.once("SIGINT", kill);
      process.once("SIGTERM", kill);
      process.once("exit", kill);
    },
  };
}
