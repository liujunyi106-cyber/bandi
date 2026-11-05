/**
 * 功能：自动更新（GitHub Releases 通道，electron-updater）。
 * 启动后静默检查一次；托盘「检查更新…」可手动触发。开发态（未打包）不启用。
 */
import { app, dialog } from "electron";
import { autoUpdater } from "electron-updater";

let initialized = false;

/** 功能：初始化更新器与事件回调（仅打包态生效）。 */
export function initUpdater(): void {
  if (initialized || !app.isPackaged) return;
  initialized = true;
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on("error", (error) => {
    console.warn("[updater] 检查更新失败：", error?.message ?? error);
  });

  autoUpdater.on("update-available", (info) => {
    void dialog.showMessageBox({
      type: "info",
      title: "发现新版本",
      message: `新版本 ${info.version} 正在后台下载，完成后会提示重启安装。`,
      buttons: ["知道了"],
    });
  });

  autoUpdater.on("update-downloaded", (info) => {
    void dialog
      .showMessageBox({
        type: "info",
        title: "更新已就绪",
        message: `新版本 ${info.version} 已下载完成，是否立即重启安装？`,
        buttons: ["立即重启", "稍后"],
        defaultId: 0,
        cancelId: 1,
      })
      .then((result) => {
        if (result.response === 0) autoUpdater.quitAndInstall();
      });
  });

  // 启动 8 秒后静默检查，避免与应用启动争抢资源
  setTimeout(() => void autoUpdater.checkForUpdates().catch(() => undefined), 8000);
}

/** 功能：托盘菜单「检查更新…」——手动检查并给出结果提示。 */
export async function checkForUpdatesInteractive(): Promise<void> {
  if (!app.isPackaged) {
    await dialog.showMessageBox({
      type: "info",
      title: "检查更新",
      message: "开发模式不检查更新；打包安装版才走发布通道。",
      buttons: ["知道了"],
    });
    return;
  }
  try {
    const result = await autoUpdater.checkForUpdates();
    if (!result?.updateInfo) {
      await dialog.showMessageBox({
        type: "info",
        title: "检查更新",
        message: "当前没有可用的更新信息。",
        buttons: ["知道了"],
      });
      return;
    }
    const latest = result.updateInfo.version;
    if (latest === app.getVersion()) {
      await dialog.showMessageBox({
        type: "info",
        title: "检查更新",
        message: `已是最新版本（${latest}）。`,
        buttons: ["知道了"],
      });
    }
  } catch (error) {
    await dialog.showMessageBox({
      type: "warning",
      title: "检查更新失败",
      message: `无法连接更新源：${error instanceof Error ? error.message : String(error)}`,
      detail: "请确认已发布 Release，且网络可访问 GitHub。",
      buttons: ["知道了"],
    });
  }
}
