/**
 * 功能：桌面壳注入的本地 API key（浏览器/dev 环境为空）。
 */
export function desktopApiKey(): string {
  const desktop = (window as unknown as { bandi?: { apiKey?: string } }).bandi;
  return typeof desktop?.apiKey === "string" ? desktop.apiKey : "";
}

/**
 * 功能：是否运行在 Electron 壳内。
 * 用于启用自绘标题栏拖拽区（主进程 titleBarOverlay 高 44px，两者必须一致）。
 */
export const isDesktopShell: boolean =
  typeof window !== "undefined" && Boolean((window as unknown as { bandi?: unknown }).bandi);
