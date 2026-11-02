/**
 * 功能：Bandi sidecar 进程管理（Day147 C-3 / @base docs/ADR.md ADR-42）。
 *
 * 职责：生产模式下 Electron 壳 spawn `bandi serve --port 0`（动态端口），
 * 等待就绪后把 `http://127.0.0.1:<port>/` 交给窗口加载；退出时树杀 sidecar
 * （含调度器等子进程），崩溃时自动重启（有限次），彻底失败降级 loadFile。
 *
 * 端口获取（双通道，与 bandi/cli/app.py 的回写口径对齐）：
 *   ① stdout 结构化行 `BANDI_SIDECAR_READY port=<N> pid=<P>`（首选，零轮询）；
 *   ② `{BANDI_HOME}/sidecar.port` JSON 轮询兜底（pid 必须等于本次 spawn 的 pid，
 *      排除上一轮壳/硬杀留下的陈旧文件）。
 * 就绪判定：`GET /ready` 返回 200（依赖探针，/health 是 liveness 口径）。
 */
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { app } from "electron";

const SIDECAR_READY_PREFIX = "BANDI_SIDECAR_READY";
const START_TIMEOUT_MS = 30_000;
const PROBE_INTERVAL_MS = 250;
const MAX_RESTARTS = 3;
const RESTART_BACKOFF_MS = [1_000, 3_000, 5_000];

/** 功能：BANDI_HOME 解析（与后端 bandi.api 同口径）。 */
export function bandiHome(): string {
  return process.env.BANDI_HOME ?? path.join(os.homedir(), ".bandi");
}

export interface SidecarCommand {
  command: string;
  args: string[];
  cwd: string;
}

/**
 * 功能：sidecar 启动命令解析（C-5 打包口径）。
 *
 * 优先级：
 *   ① `BANDI_SIDECAR_CMD` 显式覆盖（PyInstaller onedir exe 完整路径）；
 *   ② 打包态：`<resources>/sidecar/bandi-sidecar/bandi-sidecar.exe`
 *      （electron-builder resources 布局，随安装包分发）；
 *   ③ `~/.bandi/sidecar/bandi-sidecar/bandi-sidecar.exe`
 *      （外部替换目录——与 frontend dist 外部替换同风格，发版只换 sidecar）；
 *   ④ 开发态：仓库 .venv python + `python -m bandi serve`。
 * 都不可得 → null（壳降级 loadFile）。
 */
export function resolveSidecarCommand(): SidecarCommand | null {
  const override = process.env.BANDI_SIDECAR_CMD?.trim();
  if (override) {
    return {
      command: override,
      args: ["serve", "--host", "127.0.0.1", "--port", "0"],
      cwd: path.dirname(override),
    };
  }

  const exeName = process.platform === "win32" ? "bandi-sidecar.exe" : "bandi-sidecar";
  const candidates = [
    ...(app.isPackaged
      ? [
          path.join(process.resourcesPath, "sidecar", "bandi-sidecar", exeName),
          path.join(path.dirname(app.getPath("exe")), "sidecar", "bandi-sidecar", exeName),
        ]
      : []),
    path.join(bandiHome(), "sidecar", "bandi-sidecar", exeName),
  ];
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return {
        command: candidate,
        args: ["serve", "--host", "127.0.0.1", "--port", "0"],
        cwd: path.dirname(candidate),
      };
    }
  }

  // ④ 开发态：仓库 .venv（PyInstaller 未就绪前的唯一可用形态）
  if (app.isPackaged) return null;
  const repoBandi = path.join(__dirname, "..", "..", "bandi");
  const venvPython = path.join(
    repoBandi,
    ".venv",
    process.platform === "win32" ? "Scripts" : "bin",
    "python.exe"
  );
  return {
    command: venvPython,
    args: ["-m", "bandi", "serve", "--host", "127.0.0.1", "--port", "0"],
    cwd: repoBandi,
  };
}

interface SidecarPortPayload {
  port: number;
  pid: number;
}

/** 功能：读端口文件（陈旧校验：pid 必须等于当前 sidecar 的 pid）。 */
function readPortFile(child: ChildProcess, childPid: number | undefined): number | null {
  if (!childPid || child.exitCode !== null) return null;
  try {
    const payload = JSON.parse(
      readFileSync(path.join(bandiHome(), "sidecar.port"), "utf-8")
    ) as SidecarPortPayload;
    if (payload?.pid === childPid && Number.isInteger(payload.port) && payload.port > 0) {
      return payload.port;
    }
  } catch {
    // 文件缺失/半截 JSON：正常轮询窗口，忽略
  }
  return null;
}

/** 功能：/ready 探针（200 = 依赖可用；503 = 存活但依赖故障，不算就绪）。 */
async function probeReady(port: number): Promise<boolean> {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/ready`, {
      signal: AbortSignal.timeout(2_000),
    });
    return response.status === 200;
  } catch {
    return false;
  }
}

export interface SidecarHandle {
  port: number;
  pid: number;
}

type GiveUpListener = (reason: "spawn-failed" | "restart-exhausted" | "start-timeout") => void;

/**
 * 功能：sidecar 生命周期管理器。
 *
 * start()：spawn → 等端口（stdout/端口文件双通道，30s 超时）→ 等 /ready。
 * stop()：树杀（Windows taskkill /T /F 含子进程；其他平台 SIGTERM→SIGKILL）。
 * 崩溃（非 stop 触发的 exit）：退避重启，最多 MAX_RESTARTS 次；
 * 耗尽或起不来 → onGiveUp 回调，壳降级 loadFile。
 */
export class SidecarManager {
  private child: ChildProcess | null = null;
  private stopping = false;
  private restarts = 0;
  private onGiveUp: GiveUpListener | null = null;
  private port: number | null = null;

  setGiveUpListener(listener: GiveUpListener): void {
    this.onGiveUp = listener;
  }

  isRunning(): boolean {
    return this.child !== null && this.child.exitCode === null;
  }

  currentPort(): number | null {
    return this.isRunning() ? this.port : null;
  }

  async start(): Promise<SidecarHandle | null> {
    const cmd = resolveSidecarCommand();
    if (!cmd) {
      console.error(
        "[sidecar] 打包态未内置 sidecar（resources/sidecar 或 ~/.bandi/sidecar/ 均未找到），降级 loadFile"
      );
      return null;
    }
    try {
      return await this.spawnAndWait(cmd);
    } catch (error) {
      console.error("[sidecar] 启动失败：", error);
      return null;
    }
  }

  private spawnAndWait(cmd: SidecarCommand): Promise<SidecarHandle | null> {
    return new Promise((resolve) => {
      const child = spawn(cmd.command, cmd.args, {
        cwd: cmd.cwd,
        env: { ...process.env, BANDI_HOME: bandiHome() },
        windowsHide: true, // CREATE_NO_WINDOW：桌面应用不得闪黑框
        detached: false, // 跟随父进程生命周期，双保险（will-quit 还有显式树杀）
        stdio: ["ignore", "pipe", "pipe"],
      });
      this.child = child;
      this.stopping = false;

      let port: number | null = null;
      let settled = false;
      const finish = (handle: SidecarHandle | null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeoutTimer);
        clearInterval(probeTimer);
        clearInterval(fileTimer);
        this.port = handle?.port ?? null;
        resolve(handle);
      };

      // 通道 ①：stdout 结构化行（最快）
      child.stdout?.setEncoding("utf-8");
      let stdoutTail = "";
      child.stdout?.on("data", (chunk: string) => {
        stdoutTail = (stdoutTail + chunk).slice(-2000);
        for (const line of stdoutTail.split("\n")) {
          if (line.startsWith(`${SIDECAR_READY_PREFIX} port=`)) {
            const match = /^port=(\d+)\s+pid=(\d+)$/.exec(line.slice(SIDECAR_READY_PREFIX.length).trim());
            if (match && Number(match[1]) > 0) {
              port = Number(match[1]);
              break;
            }
          }
        }
      });
      // 崩溃诊断：stderr 全量透传到壳日志
      child.stderr?.setEncoding("utf-8");
      child.stderr?.on("data", (chunk: string) => process.stderr.write(`[sidecar] ${chunk}`));

      // 通道 ②：端口文件轮询兜底（stdout 丢失/缓冲被劫持时仍能拿到端口）
      const fileTimer = setInterval(() => {
        if (port === null) port = readPortFile(child, child.pid);
      }, PROBE_INTERVAL_MS);

      // /ready 就绪探针轮询
      const probeTimer = setInterval(() => {
        if (port === null) return;
        void probeReady(port).then((ok) => {
          if (ok && child.pid) finish({ port: port as number, pid: child.pid });
        });
      }, PROBE_INTERVAL_MS);

      // 立即退出（python 缺失/解释器坏）：spawn 后 exitCode 立刻有值
      child.on("exit", (code) => {
        if (this.stopping) return;
        if (!settled) {
          finish(null);
          return;
        }
        // 就绪后崩溃：退避重启
        this.handleCrash(code);
      });

      const timeoutTimer = setTimeout(() => {
        if (!settled) {
          console.error("[sidecar] 30s 内未就绪，放弃本轮（进程将被停止）");
          this.stop();
          this.onGiveUp?.("start-timeout");
          finish(null);
        }
      }, START_TIMEOUT_MS);
    });
  }

  private handleCrash(code: number | null): void {
    if (this.stopping) return;
    if (this.restarts >= MAX_RESTARTS) {
      console.error(`[sidecar] 崩溃重启次数耗尽（exitCode=${code}），降级 loadFile`);
      this.onGiveUp?.("restart-exhausted");
      return;
    }
    const backoff = RESTART_BACKOFF_MS[Math.min(this.restarts, RESTART_BACKOFF_MS.length - 1)];
    this.restarts += 1;
    console.warn(`[sidecar] 崩溃（exitCode=${code}），${backoff}ms 后第 ${this.restarts} 次重启`);
    setTimeout(() => {
      if (this.stopping) return;
      void this.start().then((handle) => {
        if (!handle) this.onGiveUp?.("spawn-failed");
      });
    }, backoff);
  }

  stop(): void {
    this.stopping = true;
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    if (process.platform === "win32") {
      // 树杀：sidecar 的调度器等子进程一并回收，不留孤儿 python 进程
      spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        windowsHide: true,
        stdio: "ignore",
      });
    } else {
      child.kill("SIGTERM");
      setTimeout(() => {
        if (child.exitCode === null) child.kill("SIGKILL");
      }, 5_000);
    }
  }
}
