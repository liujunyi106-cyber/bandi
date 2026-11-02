/**
 * 功能：桌面壳注入的本地 API key（浏览器/dev 环境为空）。
 */
export function desktopApiKey(): string {
  const desktop = (window as unknown as { bandi?: { apiKey?: string } }).bandi;
  return typeof desktop?.apiKey === "string" ? desktop.apiKey : "";
}
