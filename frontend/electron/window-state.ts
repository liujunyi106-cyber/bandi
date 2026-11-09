/**
 * 功能：主窗口位置与尺寸记忆（userData/window-state.json）。
 * 恢复时校验窗口仍落在某块屏幕的可见区域内，避免换显示器后窗口跑到屏幕外。
 */
import { app, screen } from "electron";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

export interface WindowState {
  width: number;
  height: number;
  x?: number;
  y?: number;
  maximized: boolean;
}

export const DEFAULT_WINDOW_STATE: WindowState = {
  width: 1440,
  height: 900,
  maximized: false,
};

function stateFile(): string {
  return path.join(app.getPath("userData"), "window-state.json");
}

function isVisibleOnSomeDisplay(bounds: WindowState): boolean {
  if (bounds.x === undefined || bounds.y === undefined) return false;
  return screen.getAllDisplays().some(({ workArea }) => {
    const withinX = bounds.x! >= workArea.x - 40 && bounds.x! < workArea.x + workArea.width - 80;
    const withinY = bounds.y! >= workArea.y - 40 && bounds.y! < workArea.y + workArea.height - 80;
    return withinX && withinY;
  });
}

/** 功能：读取上次窗口状态；坐标不可见时只沿用尺寸。 */
export function loadWindowState(): WindowState {
  const file = stateFile();
  if (!existsSync(file)) return { ...DEFAULT_WINDOW_STATE };
  try {
    const parsed = JSON.parse(readFileSync(file, "utf-8")) as Partial<WindowState>;
    const state: WindowState = {
      width: parsed.width ?? DEFAULT_WINDOW_STATE.width,
      height: parsed.height ?? DEFAULT_WINDOW_STATE.height,
      maximized: parsed.maximized ?? false,
    };
    if (isVisibleOnSomeDisplay({ ...state, x: parsed.x, y: parsed.y })) {
      state.x = parsed.x;
      state.y = parsed.y;
    }
    return state;
  } catch {
    return { ...DEFAULT_WINDOW_STATE };
  }
}

/** 功能：保存窗口状态（最大化时记录还原后的尺寸，便于下次还原）。 */
export function saveWindowState(win: Electron.BrowserWindow): void {
  const maximized = win.isMaximized();
  const bounds = maximized ? win.getNormalBounds() : win.getBounds();
  const state: WindowState = {
    width: bounds.width,
    height: bounds.height,
    x: bounds.x,
    y: bounds.y,
    maximized,
  };
  const file = stateFile();
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(state, null, 2), "utf-8");
}
