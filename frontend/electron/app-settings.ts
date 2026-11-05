/**
 * 功能：桌面端偏好设置持久化（userData/app-settings.json）。
 * 目前承载：开机自启开关、关窗行为（最小化到托盘 / 直接退出）。
 */
import { app } from "electron";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

export interface AppSettings {
  autoLaunch: boolean;
  closeToTray: boolean;
}

const DEFAULT_SETTINGS: AppSettings = {
  autoLaunch: false,
  closeToTray: true,
};

function settingsFile(): string {
  return path.join(app.getPath("userData"), "app-settings.json");
}

/** 功能：读取偏好设置；文件缺失或损坏时回落默认值。 */
export function loadSettings(): AppSettings {
  const file = settingsFile();
  if (!existsSync(file)) return { ...DEFAULT_SETTINGS };
  try {
    const parsed = JSON.parse(readFileSync(file, "utf-8")) as Partial<AppSettings>;
    return {
      autoLaunch: parsed.autoLaunch ?? DEFAULT_SETTINGS.autoLaunch,
      closeToTray: parsed.closeToTray ?? DEFAULT_SETTINGS.closeToTray,
    };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

/** 功能：写入偏好设置（目录缺失时创建）。 */
export function saveSettings(settings: AppSettings): void {
  const file = settingsFile();
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(settings, null, 2), "utf-8");
}
