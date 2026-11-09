/**
 * 功能：Electron 预加载脚本。
 * 安全基线：contextIsolation 开、nodeIntegration 关、sandbox 开——渲染进程只能拿到这里白名单暴露的能力。
 * 仅暴露只读运行信息；IPC 白名单通道随 桌面体验项落地。
 */
import { contextBridge } from "electron";

const desktopApi = {
  /** 平台名（渲染侧做静态分支用） */
  platform: process.platform,
  /** 只读版本信息（用于页面诊断/版本提示） */
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  /** 本地内核 API key（未配置时为空，渲染层不发鉴权头）。 */
  apiKey: process.env.BANDI_API_KEY ?? "",
} as const;

contextBridge.exposeInMainWorld("bandi", desktopApi);

export type BandiDesktopApi = typeof desktopApi;
