/**
 * 功能：系统托盘（图标 + 右键菜单：显示主窗口 / 开机自启 / 退出）。
 * 双击托盘图标显示主窗口；关闭窗口时是否留驻托盘由偏好设置决定。
 */
import { Menu, Tray, app, nativeImage } from "electron";
import path from "node:path";

export interface TrayHandlers {
  showWindow: () => void;
  quit: () => void;
  toggleAutoLaunch: (enabled: boolean) => void;
  checkUpdate: () => void;
}

function buildMenu(autoLaunch: boolean, handlers: TrayHandlers): Menu {
  return Menu.buildFromTemplate([
    { label: "显示主窗口", click: handlers.showWindow },
    { type: "separator" },
    {
      label: "开机自启",
      type: "checkbox",
      checked: autoLaunch,
      click: (item) => handlers.toggleAutoLaunch(item.checked),
    },
    { label: "检查更新…", click: handlers.checkUpdate },
    { type: "separator" },
    { label: "退出 Bandi", click: handlers.quit },
  ]);
}

/** 功能：创建托盘；图标取自打包内的 electron/assets/tray.png。 */
export function createTray(autoLaunch: boolean, handlers: TrayHandlers): Tray {
  const icon = nativeImage.createFromPath(path.join(__dirname, "assets", "tray.png"));
  const tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  tray.setToolTip("Bandi");
  tray.setContextMenu(buildMenu(autoLaunch, handlers));
  tray.on("double-click", handlers.showWindow);
  return tray;
}

/** 功能：开机自启状态变化后刷新托盘勾选态。 */
export function refreshTrayMenu(tray: Tray, autoLaunch: boolean, handlers: TrayHandlers): void {
  tray.setContextMenu(buildMenu(autoLaunch, handlers));
}

/** 功能：把开机自启开关同步到系统（Windows 注册表 Run 项）。 */
export function applyAutoLaunch(enabled: boolean): void {
  if (!app.isPackaged) return;
  app.setLoginItemSettings({ openAtLogin: enabled, path: process.execPath });
}
