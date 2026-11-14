/**
 * 功能：Electron 主进程壳（@base docs/ADR.md ADR-42 / docs/前端设计规划.md §12）。
 * dev：加载 vite dev server（VITE_DEV_SERVER_URL），三进程并行（vite + uvicorn + 本窗口）。
 * 生产：spawn FastAPI sidecar → /ready → loadURL（SPA + API + WS 单端口）；失败降级 loadFile。
 * 桌面体验：托盘、窗口状态记忆、开机自启、通知权限。
 */
import { BrowserWindow, Menu, Tray, app, session, shell } from "electron";
import path from "node:path";

import { loadSettings, saveSettings } from "./app-settings";
import { SidecarManager } from "./sidecar";
import { applyAutoLaunch, createTray, refreshTrayMenu } from "./tray";
import { checkForUpdatesInteractive, initUpdater } from "./updater";
import { loadWindowState, saveWindowState } from "./window-state";

const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;
const APP_USER_MODEL_ID = "com.bandi.desktop";

/** 功能：启动阶段广播通道名（与 preload / 渲染层共用同一常量）。 */
const BOOT_STAGE_CHANNEL = "bandi:boot-stage";

/** sidecar 生命周期管理器（生产模式唯一实例；dev 模式不用）。 */
const sidecar = new SidecarManager();
const settings = loadSettings();

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;

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

/** 功能：显示并聚焦主窗口（托盘点击、第二实例、激活事件共用）。 */
function showMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

/** 功能：把偏好设置里的开机自启写入系统并刷新托盘勾选。 */
function syncAutoLaunch(enabled: boolean): void {
  settings.autoLaunch = enabled;
  saveSettings(settings);
  applyAutoLaunch(enabled);
  if (tray) refreshTrayMenu(tray, enabled, trayHandlers);
}

const trayHandlers = {
  showWindow: showMainWindow,
  quit: () => {
    quitting = true;
    app.quit();
  },
  toggleAutoLaunch: syncAutoLaunch,
  checkUpdate: () => void checkForUpdatesInteractive(),
};

/** 功能：创建主窗口（尺寸与位置取自上次退出时的记忆）。 */
function createWindow(): void {
  const state = loadWindowState();
  const win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: "#f7f8fa",
    autoHideMenuBar: true,
    // 自绘标题栏：44px 拖拽区（渲染层 index.css 的 --titlebar-h 必须同值），
    // 由系统只画右上角三个控制按钮，底色与页面一致、不显示图标与标题。
    ...(process.platform === "win32"
      ? {
          titleBarStyle: "hidden" as const,
          titleBarOverlay: { color: "#ffffff", symbolColor: "#111111", height: 44 },
        }
      : {}),
    icon: path.join(__dirname, "assets", "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow = win;

  // 启动阶段广播：占位页（file: 静态壳）在 ready-to-show 前就开始等，
  // 所以订阅时机要早于 loadFile，且窗口重建时重新挂载。
  sidecar.setStageListener((stage) => {
    if (win.isDestroyed()) return;
    win.webContents.send(BOOT_STAGE_CHANNEL, stage);
  });

  win.once("ready-to-show", () => {
    if (state.maximized) win.maximize();
    win.show();
  });

  // 关窗即最小化到托盘（可在偏好里改），托盘「退出」才真正结束进程
  win.on("close", (event) => {
    saveWindowState(win);
    if (!quitting && settings.closeToTray) {
      event.preventDefault();
      win.hide();
    }
  });

  let saveTimer: NodeJS.Timeout | null = null;
  const scheduleSave = () => {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      if (!win.isDestroyed()) saveWindowState(win);
    }, 400);
  };
  win.on("resize", scheduleSave);
  win.on("move", scheduleSave);

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
            "default-src 'self'; font-src 'self' data:; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
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
  // 就绪前先显示启动占位页，避免「内核未连接却已展示完整界面」造成误判；
  // sidecar 起不来/超时/崩溃耗尽 → 占位页转为带错误原因的重试页（不白屏、不假装可用）。
  void win.loadFile(path.join(__dirname, "..", "dist", "index.html"), {
    query: { boot: "starting" },
  });
  void sidecar.start().then((handle) => {
    if (handle) {
      void win.loadURL(`http://127.0.0.1:${handle.port}/`);
      return;
    }
    console.warn("[main] sidecar 不可用，占位页转为错误重试页（不假装载入）");
    if (win.isDestroyed()) return;
    void win.loadFile(path.join(__dirname, "..", "dist", "index.html"), {
      query: { boot: "failed" },
    });
  });
}

/** 功能：sidecar 彻底失败后的窗口降级（loadURL 已发出的场景重载静态壳）。 */
function degradeWindowToStatic(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const current = mainWindow.webContents.getURL();
  if (current.startsWith("http://127.0.0.1:")) {
    void mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
}

/** 功能：只对本地 sidecar 页面放行通知权限（桌面通知与提示音依赖它）。 */
function allowNotificationsForLocalApp(): void {
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback) => {
    const url = safeUrl(contents.getURL());
    const isLocalApp = url?.hostname === "127.0.0.1" || url?.hostname === "localhost";
    callback(permission === "notifications" && Boolean(isLocalApp));
  });
}

// 单实例锁：重复启动时聚焦既有窗口
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.setAppUserModelId(APP_USER_MODEL_ID);
  // 功能：主进程未捕获异常只记日志，不弹「A JavaScript error occurred」模态框。
  // 打包态没有终端可看，模态框会卡住整个窗口且只能反复点确定；壳的降级路径
  // （sidecar 失败 → 静态壳）本身可继续使用，故记录后保持运行。
  process.on("uncaughtException", (error) => {
    console.error("[main] uncaughtException:", error);
  });
  process.on("unhandledRejection", (reason) => {
    console.error("[main] unhandledRejection:", reason);
  });
  // 功能：移除 Electron 默认应用菜单（应用/编辑），桌面端只用自绘标题栏与托盘菜单。
  Menu.setApplicationMenu(null);

  app.on("second-instance", showMainWindow);

  void app.whenReady().then(() => {
    // sidecar 崩溃重启耗尽 → 已加载的 http 页面降级为静态壳
    sidecar.setGiveUpListener(() => degradeWindowToStatic());
    allowNotificationsForLocalApp();
    createWindow();
    applyAutoLaunch(settings.autoLaunch);
    tray = createTray(settings.autoLaunch, trayHandlers);
    tray.on("click", showMainWindow);
    initUpdater();
    app.on("activate", showMainWindow);
  });

  app.on("before-quit", () => {
    quitting = true;
  });

  app.on("will-quit", () => {
    // 退出时树杀 sidecar（含调度器子进程），不留孤儿 python 进程
    sidecar.stop();
  });

  // 关闭到托盘时窗口只是隐藏，不能因 window-all-closed 退出
  app.on("window-all-closed", () => {
    if (quitting || !settings.closeToTray) app.quit();
  });
}
