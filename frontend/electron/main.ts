/**
 * 功能：Electron 主进程壳（@base docs/ADR.md ADR-42 / docs/前端设计规划.md §12）。
 * dev：加载 vite dev server（VITE_DEV_SERVER_URL），三进程并行（vite + uvicorn + 本窗口）。
 * 生产：加载 `dist/index.html`（由壳内 spawn 的 FastAPI sidecar 托管亦可，P5-6 定）。
 * 托盘 / 窗口状态记忆 / 自启动 / `/ready` 就绪探针后开窗 → P5-6 细化。
 */
import { BrowserWindow, app, session, shell } from "electron";
import path from "node:path";

import { SidecarManager } from "./sidecar";

const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

/** sidecar 生命周期管理器（生产模式唯一实例；dev 模式不用）。 */
const sidecar = new SidecarManager();

function safeUrl(value: string): URL | null {
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

function openExternalSafely(value: string): void {
  const target = safeUrl(value);
  if (!target) return;
  if (!["http:", "https:", "mailto:"].includes(target.protocol)) return;
  void shell.openExternal(value);
}

/** 功能：创建主窗口（1440×900，最小 1024×700 —— §8 窗口与断点）。 */
function createWindow(): void {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: "#f7f8fa",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once("ready-to-show", () => win.show());

  // 外链一律交给系统浏览器，不在壳内开新窗口；仅放行 http/https/mailto。
  win.webContents.setWindowOpenHandler(({ url }) => {
    openExternalSafely(url);
    return { action: "deny" };
  });

  // 顶层表单/链接导航不得把主窗口带到外站（preload 会随远端页面执行）。
  win.webContents.on("will-navigate", (event, url) => {
    const target = safeUrl(url);
    const current = safeUrl(win.webContents.getURL());
    if (!target) {
      event.preventDefault();
      return;
    }
    if (current && target.origin === current.origin) return;
    event.preventDefault();
    openExternalSafely(url);
  });

  if (!DEV_SERVER_URL) {
    session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
      callback({
        responseHeaders: {
          ...(details.responseHeaders ?? {}),
          "Content-Security-Policy": [
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
              "img-src 'self' data: blob: https:; connect-src 'self' http://127.0.0.1:* ws://127.0.0.1:*; " +
              "worker-src 'self' blob:; frame-src 'none'; object-src 'none'; base-uri 'self'",
          ],
        },
      });
    });
  }

  if (DEV_SERVER_URL) {
    void win.loadURL(DEV_SERVER_URL);
    win.webContents.openDevTools({ mode: "detach" });
    return;
  }

  // 生产模式（ADR-42）：spawn sidecar → /ready → loadURL（SPA+API+WS 单端口）。
  // sidecar 起不来/超时/崩溃耗尽 → 降级 loadFile（静态壳，不白屏）。
  void sidecar.start().then((handle) => {
    if (handle) {
      void win.loadURL(`http://127.0.0.1:${handle.port}/`);
      return;
    }
    console.warn("[main] sidecar 不可用，降级 loadFile（file:// 静态壳）");
    void win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  });
}

/** 功能：sidecar 彻底失败后的窗口降级（loadURL 已发出的场景重载静态壳）。 */
function degradeWindowToStatic(): void {
  const [win] = BrowserWindow.getAllWindows();
  if (!win || win.isDestroyed()) return;
  const current = win.webContents.getURL();
  if (current.startsWith("http://127.0.0.1:")) {
    void win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

// 单实例锁：重复启动时聚焦既有窗口
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const [existing] = BrowserWindow.getAllWindows();
    if (!existing) return;
    if (existing.isMinimized()) existing.restore();
    existing.focus();
  });

  void app.whenReady().then(() => {
    // sidecar 崩溃重启耗尽 → 已加载的 http 页面降级为静态壳
    sidecar.setGiveUpListener(() => degradeWindowToStatic());
    createWindow();
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on("will-quit", () => {
    // 退出时树杀 sidecar（含调度器子进程），不留孤儿 python 进程
    sidecar.stop();
  });

  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
}
